"""Telegram companion process for the crosschain paper trader. PAPER TRADING ONLY.
- While no CROSSCHAIN_TELEGRAM_TOKEN exists (env or box-secrets.json), it only waits: alerts keep queueing in the
  outbox table, nothing is sent. It re-checks for the token every 60 s, so no restart is needed once Sammy adds it.
- Once a token exists: Sammy opens the new bot in Telegram and presses Start; that chat id is saved and is the
  only chat that ever gets messages or answers. Queued alerts older than [telegram].max_backlog_age_hours are
  marked 'expired' instead of flooding the chat.
The token is read into memory only and is never printed or logged. Memebot's/copytrader's tokens are never used."""
import json, logging, os, sys, time
from datetime import datetime
import requests
from common import BASE, db, init_db, load_config, get_state, set_state, now_ts, TZ
import notify, trader

CHAT_FILE = os.path.join(BASE, "telegram_chat.json")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("telegram")
HELP = ("ℹ️ <b>Crosschain commands</b>\n"
        "Paper trading only (fake money).\n\n"
        "/status - balance, profit/loss and each chain (both accounts: Current scoring and New scoring)\n"
        "/positions - open trades with current profit/loss (both accounts)\n"
        "/pause - stop opening new trades in both accounts (open ones still follow their plan)\n"
        "/resume - allow new trades again\n"
        "/help - this list")

class TG:
    def __init__(self):
        self.s = requests.Session()
    def call(self, method, http_timeout=30, **params):
        tok = notify.telegram_token()
        if not tok:
            raise RuntimeError("no token")
        try:
            return self.s.post(f"https://api.telegram.org/bot{tok}/{method}", json=params, timeout=http_timeout).json()
        except Exception as e:
            raise RuntimeError(notify.redact(f"{type(e).__name__}: {e}")) from None

def load_chat():
    try:
        with open(CHAT_FILE) as f:
            return json.load(f).get("chat_id")
    except Exception:
        return None

def save_chat(chat):
    with open(CHAT_FILE, "w") as f:
        json.dump({"chat_id": chat["id"], "username": chat.get("username"), "first_name": chat.get("first_name"),
                   "saved_at": datetime.now(TZ).isoformat(timespec="seconds")}, f, indent=2)

ACCOUNTS = (("current", "Current scoring"), ("new", "New scoring"))

def _new_active(con):
    return get_state(con, trader.skey("new", "cash")) is not None

def _accounts(con):
    return [a for a in ACCOUNTS if a[0] == "current" or _new_active(con)]

def _open_rows(con, cfg, acct="current"):
    out = []
    for p in con.execute("SELECT * FROM positions WHERE status='open' AND scoring=? ORDER BY opened_at", (acct,)).fetchall():
        val = (p["proceeds_usd"] or 0) + trader.position_value(cfg, p)
        out.append((p, val - p["cost_usd"], (val / p["cost_usd"] - 1) * 100))
    return out

def positions_text(con, cfg):
    """Open trades of BOTH paper accounts, one section each."""
    parts, total = [], 0
    for acct, label in _accounts(con):
        rows = _open_rows(con, cfg, acct)
        total += len(rows)
        blocks = []
        for p, pnl, pct in rows:
            icon = "🟢" if pnl >= 0 else "🔴"
            b = [f"{icon} <b>{notify.esc(p['symbol'])}</b> · {notify.esc(notify.chain_name(p['chain']))}",
                 f"<b>Now:</b> {notify.fmt_signed_usd(pnl)} ({notify.fmt_pct(pct)})",
                 f"<b>Bought:</b> {notify.fmt_usd(p['cost_usd'])} at {notify.fmt_price(p['entry_price'])}",
                 f"<b>Price now:</b> {notify.fmt_price(p['last_price'] or 0)}",
                 f"<b>Held for:</b> {notify.fmt_duration(now_ts() - p['opened_at'])}"
                 + (" · half already sold" if p["tp1_done"] else "")]
            if p["url"]:
                b.append(notify.chart_link(p["url"]))
            blocks.append("\n".join(b))
        parts.append(f"🏷 <b>{label}</b> ({len(rows)} open)\n\n" + ("\n\n".join(blocks) if blocks else "No open paper trades right now."))
    return f"📂 <b>Open trades ({total})</b>\n\n" + "\n\n".join(parts)

def _account_lines(con, cfg, acct, label):
    eq = trader.equity(con, cfg, acct)
    start = get_state(con, trader.skey(acct, "starting_balance"), trader.start_balance(cfg, acct))
    day_start = trader.day_start_equity(con, acct, eq)
    mp = get_state(con, "manual_pause") or {}
    entries = ("⏸ paused (you sent /pause)" if mp.get("on") else
               "⛔ paused for today (daily loss limit)" if get_state(con, trader.skey(acct, "paused_today"), False) else "✅ allowed")
    L = [f"🏷 <b>{label}</b>",
         f"<b>Balance:</b> {notify.fmt_usd(eq)} (fake money)",
         f"<b>Total:</b> {notify.fmt_signed_usd(eq - start)} ({notify.fmt_pct((eq / start - 1) * 100, 2)})",
         f"<b>Today:</b> {notify.fmt_signed_usd(eq - day_start)}",
         f"<b>New trades:</b> {entries}",
         "<b>By chain</b>"]
    stats = {r["chain"]: r for r in con.execute("""SELECT chain, COUNT(*) n, SUM(status='open') o,
                SUM(pnl_usd) p, SUM(status='closed' AND pnl_usd>0) w, SUM(status='closed') c FROM positions WHERE scoring=? GROUP BY chain""", (acct,))}
    for ch, cc in (cfg.get("chains") or {}).items():
        name = notify.esc(notify.chain_name(ch))
        if not cc.get("enabled"):
            L.append(f"{name}: switched off"); continue
        r = stats.get(ch)
        if not r:
            L.append(f"{name}: no trades yet"); continue
        L.append(f"{name}: {r['n']} trade{'s' if r['n'] != 1 else ''} ({r['o'] or 0} open) · {notify.fmt_signed_usd(r['p'] or 0)}"
                 + (f" · {r['w'] or 0} of {r['c']} closed were wins" if r["c"] else ""))
    rows = _open_rows(con, cfg, acct)
    L.append(f"<b>Open trades ({len(rows)})</b>")
    if not rows:
        L.append("None right now.")
    for p, pnl, pct in rows:
        L.append(f"{'🟢' if pnl >= 0 else '🔴'} {notify.esc(p['symbol'])} ({notify.esc(notify.chain_name(p['chain']))}): "
                 f"{notify.fmt_signed_usd(pnl)} ({notify.fmt_pct(pct)})")
    return L

def status_text(con, cfg):
    """Both paper accounts (side-by-side scoring test), then the shared scanner state."""
    st = get_state(con, "scanner_status", {}) or {}
    last = st.get("last_run")
    L = ["📊 <b>Crosschain status</b> (paper trading)",
         f"<b>Last scan:</b> " + (("just now" if now_ts() - last < 60 else f"{notify.fmt_duration(now_ts() - last)} ago") if last else "not yet")]
    ns = get_state(con, "scoring_new_status") or {}
    if ns and not ns.get("ok"):
        L.append("⚠️ New scoring is OFF (model self-test failed)")
    for acct, label in _accounts(con):
        L += [""] + _account_lines(con, cfg, acct, label)
    return "\n".join(L)

def handle(con, cfg, text):
    cmd = (text or "").strip().split()[0].split("@")[0].lower() if (text or "").strip() else ""
    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/status":
        return status_text(con, cfg)
    if cmd == "/positions":
        return positions_text(con, cfg)
    if cmd == "/pause":
        set_state(con, "manual_pause", {"on": True, "since": now_ts(), "by": "telegram"}); con.commit()
        return "⏸ <b>Paused</b>\n\nNo new paper trades will be opened (Current scoring and New scoring).\nOpen trades still follow their plan.\nSend /resume to undo."
    if cmd == "/resume":
        set_state(con, "manual_pause", {"on": False, "since": now_ts(), "by": "telegram"}); con.commit()
        return "▶️ <b>Resumed</b>\n\nNew paper trades are allowed again (Current scoring and New scoring)."
    if cmd.startswith("/"):
        return "I don't know that command.\n\n" + HELP
    return None

def send(tg, chat_id, text, is_html=True):
    """Send as HTML; if Telegram rejects the HTML, send the same message as plain text."""
    if not is_html:
        return tg.call("sendMessage", chat_id=chat_id, text=text[:4000], disable_web_page_preview=True)
    j = tg.call("sendMessage", chat_id=chat_id, text=text[:4000], parse_mode="HTML", disable_web_page_preview=True)
    if not j.get("ok") and "parse" in str(j.get("description", "")).lower():
        log.warning("Telegram rejected the HTML (%s); sending plain text instead", notify.redact(j.get("description")))
        j = tg.call("sendMessage", chat_id=chat_id, text=notify.to_plain(text)[:4000], disable_web_page_preview=True)
    return j

def drain_outbox(tg, con, chat_id, cfg):
    max_age = cfg.get("telegram", {}).get("max_backlog_age_hours", 6) * 3600
    notify.ensure(con)
    con.execute("UPDATE outbox SET status='expired' WHERE status='pending' AND created<?", (now_ts() - max_age,))
    con.commit()
    for r in con.execute("SELECT * FROM outbox WHERE status='pending' AND next_try<=? ORDER BY id LIMIT 10", (now_ts(),)).fetchall():
        try:
            j = send(tg, chat_id, r["text"], is_html=bool(r["html"]))
        except Exception as e:
            j = {"ok": False, "description": str(e)}
        if j.get("ok"):
            con.execute("UPDATE outbox SET status='sent', sent_at=?, attempts=attempts+1 WHERE id=?", (now_ts(), r["id"]))
        else:
            att = r["attempts"] + 1
            wait = max(((j.get("parameters") or {}).get("retry_after") or 0), min(600, 5 * 2 ** att))
            con.execute("UPDATE outbox SET attempts=?, next_try=?, status=?, error=? WHERE id=?",
                        (att, now_ts() + wait, "failed" if att >= 8 else "pending", notify.redact(j.get("description"))[:300], r["id"]))
            con.commit()
            break
        con.commit()
        time.sleep(1.1)

def main():
    init_db()
    tg = TG()
    con = db()
    offset = get_state(con, "tg_offset")
    backoff, said_waiting = 1, False
    while True:
        try:
            cfg = load_config()
            enabled = cfg.get("telegram", {}).get("enabled", True)
            if not enabled or not notify.telegram_token():
                set_state(con, "tg_status", {"state": "disabled" if not enabled else "waiting_for_token", "ts": now_ts()}); con.commit()
                if not said_waiting:
                    log.info("Telegram sending DISABLED: %s. Alerts are queued in the outbox table only.",
                             "telegram.enabled=false" if not enabled else "no CROSSCHAIN_TELEGRAM_TOKEN yet")
                    said_waiting = True
                time.sleep(60)
                continue
            said_waiting = False
            chat_id = load_chat()
            params = {"timeout": 10, "allowed_updates": ["message"]}
            if offset:
                params["offset"] = offset
            j = tg.call("getUpdates", 40, **params)
            if not j.get("ok"):
                raise RuntimeError(notify.redact(j.get("description")))
            for u in j.get("result", []):
                offset = u["update_id"] + 1
                m = u.get("message") or {}
                chat = m.get("chat") or {}
                text = m.get("text") or ""
                if not chat_id and chat.get("type") == "private" and text.strip().lower().startswith("/start"):
                    save_chat(chat); chat_id = chat["id"]
                    log.info("saved chat id from first /start")
                    notify.enqueue(con, f"connected:{chat_id}", "✅ <b>Crosschain alerts connected</b>\nPaper trading only (fake money).\n\n" + HELP)
                    con.commit()
                    continue
                if chat_id and chat.get("id") == chat_id:
                    reply = handle(con, cfg, text)
                    if reply:
                        send(tg, chat_id, reply)
            set_state(con, "tg_offset", offset)
            set_state(con, "tg_status", {"state": "connected" if chat_id else "waiting_for_start", "ts": now_ts()}); con.commit()
            if chat_id:
                drain_outbox(tg, con, chat_id, cfg)
            backoff = 1
        except Exception as e:
            log.error("telegram loop error: %s (retry in %ss)", notify.redact(e), backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)

if __name__ == "__main__":
    main()
