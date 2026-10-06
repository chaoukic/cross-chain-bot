"""Telegram alert outbox + message formatting for the crosschain paper trader. PAPER TRADING ONLY.

Trading code calls notify.enqueue(...), which only inserts a row into SQLite (fast, never raises), so Telegram
problems can never block trading. telegram_bot.py drains the outbox once CROSSCHAIN_TELEGRAM_TOKEN exists
(env var or /home/box/agent-data/box-secrets.json). Memebot's/copytrader's tokens are never used.

Messages are Telegram HTML (parse_mode=HTML). Every piece of outside text (token symbols, reasons, links) goes
through esc(). If Telegram ever rejects the HTML, telegram_bot.py re-sends the same message as plain text
(to_plain()). Each event has a unique key, so restarts never resend old events; "started" and crash-restart
alerts are collapsed to at most one per 10 minutes.

CLI (used by the shell scripts):  python notify.py event <key> <text>
"""
import html, json, os, re, sqlite3, sys, time
from datetime import datetime
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "crosschain.db")
TOKEN_NAME = "CROSSCHAIN_TELEGRAM_TOKEN"
SECRET_FILES = ("/home/box/agent-data/box-secrets.json", "/home/box/sand-data/box-secrets.json")
CHAIN_LABEL = {"solana": "Solana", "bsc": "BSC", "robinhood": "Robinhood Chain"}
TZ = ZoneInfo("America/Toronto")
COLLAPSE_SEC = 600          # at most one "started" / per-process crash alert per 10 minutes
COMPONENT = {"bot": "scanner", "web": "dashboard", "telegram": "Telegram alert sender"}

OUTBOX_SQL = """CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, text TEXT, created REAL,
  sent_at REAL, attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, status TEXT DEFAULT 'pending', error TEXT)"""

def ensure(con):
    con.execute(OUTBOX_SQL)
    if "html" not in [r[1] for r in con.execute("PRAGMA table_info(outbox)")]:
        con.execute("ALTER TABLE outbox ADD COLUMN html INTEGER DEFAULT 0")   # old rows = plain text

def _collapse_prefix(key):
    if key.startswith("start:"):
        return "start:"
    if key.startswith("restart:"):
        parts = key.split(":")
        return ":".join(parts[:2]) + ":"
    return None

def enqueue(con, key, text, is_html=True):
    """Queue a message (HTML by default). Never raises; duplicate keys are ignored."""
    try:
        ensure(con)
        pre = _collapse_prefix(key)
        if pre and con.execute("SELECT 1 FROM outbox WHERE key LIKE ? AND created>?", (pre + "%", time.time() - COLLAPSE_SEC)).fetchone():
            return
        con.execute("INSERT OR IGNORE INTO outbox(key,text,created,html) VALUES(?,?,?,?)", (key, text, time.time(), 1 if is_html else 0))
    except Exception:
        pass

def enqueue_standalone(key, text):
    try:
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute("PRAGMA busy_timeout=30000")
        enqueue(con, key, text)
        con.commit()
        con.close()
    except Exception:
        pass

# ---------------------------------------------------------------- small helpers (pure)
def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)

def to_plain(h):
    """HTML message -> plain text (fallback when Telegram rejects the HTML)."""
    s = re.sub(r'<a href="([^"]*)">([^<]*)</a>', lambda m: f"{m.group(2)}: {html.unescape(m.group(1))}", h)
    return html.unescape(re.sub(r"<[^>]+>", "", s))

def fmt_usd(v):
    return ("−$" if v < 0 else "$") + f"{abs(v):,.2f}"

def fmt_signed_usd(v):
    return ("+$" if v >= 0 else "−$") + f"{abs(v):,.2f}"

def fmt_pct(v, d=1):
    return ("+" if v >= 0 else "−") + f"{abs(v):.{d}f}%"

def fmt_price(v):
    if not v:
        return "$0"
    if v >= 1000:
        return f"${v:,.2f}"
    if v >= 1:
        return f"${v:,.4f}"
    import math
    decimals = min(12, -int(math.floor(math.log10(v))) + 3)   # 4 significant digits, never scientific notation
    return "$" + f"{v:.{decimals}f}"

def fmt_duration(sec):
    m = int(max(sec, 0) // 60)
    d, rem = divmod(m, 1440)
    h, mm = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {mm}m"
    return f"{mm}m"

def fmt_when(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%b %-d, %H:%M ET")

def chain_name(c):
    return CHAIN_LABEL.get(c, c or "?")

def coin_type(bucket, age_min=None):
    if bucket == "established":
        return "Established coin (older than 24h)"
    return "New launch (less than 24h old" + (f", about {fmt_duration(age_min * 60)} old)" if age_min else ")")

def chart_link(url):
    return f'<a href="{esc(url)}">View chart</a>' if url else ""

def _frac_words(frac):
    return "half" if abs(frac - 0.5) < 1e-6 else f"{frac * 100:.0f}%"

def plan_lines(entry, B, tp1_done=False, opened_at=None):
    """The exit plan in plain words. B = the age-bucket settings from config.toml."""
    L = []
    if not tp1_done:
        L.append(f"🎯 Sell {_frac_words(B['take_profit_sell_fraction'])} at {fmt_price(entry * (1 + B['take_profit_pct'] / 100))} (+{B['take_profit_pct']:g}%)")
        L.append(f"🎯 Sell the rest at {fmt_price(entry * (1 + B['take_profit2_pct'] / 100))} (+{B['take_profit2_pct']:g}%)")
    else:
        L.append(f"🎯 Sell the rest at {fmt_price(entry * (1 + B['take_profit2_pct'] / 100))} (+{B['take_profit2_pct']:g}%)")
    L.append(f"🛑 Stop loss at {fmt_price(entry * (1 - B['stop_loss_pct'] / 100))} (−{B['stop_loss_pct']:g}%)")
    L.append(f"📉 Once up {B['trailing_activate_pct']:g}%, sell if it falls {B['trailing_stop_pct']:g}% from its peak")
    hold = B["max_hold_hours"]
    hold_txt = f"{hold / 24:g} days" if hold >= 48 else f"{hold:g} hours"
    L.append(f"⏱ Max hold {hold_txt}" + (f" (until {fmt_when(opened_at + hold * 3600)})" if opened_at and tp1_done else ""))
    return L

def why_text(timing, fallback=None):
    """Plain-English reason for the entry, from the good-entry timing numbers."""
    t = timing or {}
    if t.get("pullback_pct") is not None and t.get("rise_before_pct") is not None:
        rise = t["rise_before_pct"]
        rise_s = f"{rise:.0f}%" if rise >= 10 else f"{rise:.1f}%"
        return (f"It dropped {t['pullback_pct']:.1f}% after rising {rise_s}, and has started bouncing back"
                + (" with trading picking up again." if (t.get("vol_recover_ratio") or 0) >= 1 else "."))
    return fallback or "It passed all safety checks and the entry rules."

_REASON_RULES = [
    (r"^rug \((.*)\)$", lambda m: f"RUG: {m.group(1)}"),
    (r"^stop loss \((-?[\d.]+)%", lambda m: f"Stop loss hit (price fell {abs(float(m.group(1))):.1f}% below the buy price)"),
    (r"^take profit 1 \(\+([\d.]+)%", lambda m: f"Hit first profit target (+{float(m.group(1)):.1f}%)"),
    (r"^take profit 2 \(\+([\d.]+)%", lambda m: f"Hit final profit target (+{float(m.group(1)):.1f}%)"),
    (r"^trailing stop \(([\d.]+)% off peak", lambda m: f"Trailing stop: price fell {float(m.group(1)):.1f}% from its peak"),
    (r"^volume decay", lambda m: "Volume dried up (trading activity fell sharply)"),
    (r"^max hold time", lambda m: "Max hold time reached"),
]

def plain_reason(reason):
    r = (reason or "").strip()
    for pat, f in _REASON_RULES:
        m = re.match(pat, r)
        if m:
            return f(m)
    return r[:1].upper() + r[1:] if r else "Exit rule triggered"

# ---------------------------------------------------------------- side-by-side test: which paper account an alert is for
ACCOUNT_LABEL = {"current": "OLD scoring", "new": "NEW scoring"}

def account_line(account, noun="trade"):
    """Short first line naming the paper account ('Current scoring' / 'New scoring'); empty when not given."""
    if not account:
        return []
    return [f"🏷 <b>{esc(ACCOUNT_LABEL.get(account, account))}</b> {noun}"]

def scores_line(scores):
    if not scores:
        return []
    cur, new = scores
    return [f"<b>Scores:</b> old {esc(cur if cur is not None else '—')} · new {esc(new if new is not None else '—')}"]

# ---------------------------------------------------------------- alert formatters (return Telegram HTML)
def fmt_buy(chain, bucket, symbol, price, size, B, balance, url=None, timing=None, age_min=None, fallback_why=None,
            example=False, account=None, scores=None):
    L = []
    if example:
        L += ["🧪 <b>EXAMPLE ONLY, no trade happened</b>", ""]
    L += account_line(account)
    L += [f"🟢 <b>BOUGHT {esc(symbol)}</b> (paper trade)",
          f"<b>Chain:</b> {esc(chain_name(chain))}",
          f"<b>Type:</b> {esc(coin_type(bucket, age_min))}",
          "",
          f"<b>Amount:</b> {fmt_usd(size)}",
          f"<b>Entry price:</b> {fmt_price(price)}",
          "",
          f"<b>Why:</b> {esc(why_text(timing, fallback_why))}",
          *scores_line(scores),
          "",
          "<b>Plan:</b>"] + [esc(x) for x in plan_lines(price, B)] + [
          "",
          f"<b>Balance:</b> {fmt_usd(balance)} (fake money)"]
    if url:
        L.append(chart_link(url))
    return "\n".join(L)

def fmt_sell(chain, symbol, entry_price, exit_price, reason, pnl_usd, pnl_pct, held_sec, balance, day_pnl,
             partial=False, sold_frac=1.0, left_value=None, B=None, opened_at=None, url=None, example=False,
             after_partial=False, received_usd=None, fill_price=None, capped=False, account=None):
    """Full exit: pnl_* = whole trade. Partial: pnl_* = the part just sold.
    received_usd / fill_price / capped: the modelled paper fill (pool-limited); a 'rug (...)' reason gets a RUG headline."""
    win = pnl_usd >= 0
    icon, word = ("✅", "PROFIT") if win else ("🔴", "LOSS")
    is_rug = (reason or "").startswith("rug")
    what = f"SOLD {'HALF OF ' if partial and abs(sold_frac - 0.5) < 1e-6 else ''}{esc(symbol)}"
    if partial and abs(sold_frac - 0.5) >= 1e-6:
        what = f"SOLD {sold_frac * 100:.0f}% OF {esc(symbol)}"
    L = []
    if example:
        L += ["🧪 <b>EXAMPLE ONLY, no trade happened</b>", ""]
    L += account_line(account)
    if is_rug:
        L += [f"🚨 <b>RUG — {esc(symbol)} sold for {fmt_usd(received_usd or 0)}</b>",
              "The pool's money was pulled (or the pool vanished), so the bot sold everything at once.", ""]
    L += [f"{icon} <b>{what} · {fmt_signed_usd(pnl_usd)} ({fmt_pct(pnl_pct)})</b>",
          f"{word}" + (f" on the {'half' if abs(sold_frac - 0.5) < 1e-6 else 'part'} sold" if partial else "")
          + " (paper trade, after trading costs)",
          "",
          f"<b>Chain:</b> {esc(chain_name(chain))}",
          f"<b>Bought at:</b> {fmt_price(entry_price)}",
          f"<b>Sold at:</b> {fmt_price(exit_price)}"
          + (f" (pool could only pay about {fmt_price(fill_price)})" if capped and fill_price is not None else ""),
          *( [f"<b>Received:</b> {fmt_usd(received_usd)} (fake money)"] if received_usd is not None else []),
          f"<b>Sold:</b> " + (f"{'Half' if abs(sold_frac - 0.5) < 1e-6 else f'{sold_frac * 100:.0f}%'} of the position" if partial else "All remaining"),
          f"<b>Held for:</b> {fmt_duration(held_sec)}",
          f"<b>Reason:</b> {esc(plain_reason(reason))}"]
    if partial and B:
        L += ["",
              f"<b>Still holding:</b> the other {'half' if abs(sold_frac - 0.5) < 1e-6 else f'{(1 - sold_frac) * 100:.0f}%'}"
              + (f" (worth {fmt_usd(left_value)} now)" if left_value is not None else ""),
              "<b>Plan for the rest:</b>"] + [esc(x) for x in plan_lines(entry_price, B, tp1_done=True, opened_at=opened_at)]
    elif after_partial:
        L += ["", "The result above is for the whole trade, including the earlier partial sale."]
    L += ["",
          f"<b>Balance:</b> {fmt_usd(balance)} (fake money)",
          f"<b>Today:</b> {fmt_signed_usd(day_pnl)}"]
    if url:
        L.append(chart_link(url))
    return "\n".join(L)

def fmt_pause(day_pnl, cap_usd, cap_pct, account=None):
    return "".join(x + "\n" for x in account_line(account, "account")) + (f"⛔ <b>Trading paused for today</b>\n\n"
            f"Today's loss reached {fmt_usd(abs(day_pnl))}, which hits the daily limit of {fmt_usd(cap_usd)} ({cap_pct:g}% of the balance).\n\n"
            f"No new trades until midnight (Toronto time).\nOpen trades are still watched and will sell by their plan.")

def fmt_resume(account=None):
    return "".join(x + "\n" for x in account_line(account, "account")) + "▶️ <b>Trading resumed</b>\n\nIt's a new day, so the daily loss limit has reset. New paper trades are allowed again."

def fmt_started():
    return f"🟢 <b>Crosschain bot started</b>\n\nScanning Solana, BSC and Robinhood Chain.\nPaper trading only (fake money).\n{fmt_when(time.time())}"

def fmt_stopped():
    return f"⏹ <b>Crosschain bot stopped</b>\n\nNo scanning or paper trading until it's started again.\n{fmt_when(time.time())}"

def fmt_crash(name):
    comp = COMPONENT.get(name, name)
    return f"⚠️ The {esc(comp)} crashed and restarted itself. Trading continues normally."

# ---------------------------------------------------------------- token (never printed)
_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")

def redact(s):
    return _TOKEN_RE.sub("bot<redacted>", str(s))

def telegram_token():
    """Only CROSSCHAIN_TELEGRAM_TOKEN; kept in memory, never printed."""
    t = os.environ.get(TOKEN_NAME)
    if not t:
        for path in SECRET_FILES:
            try:
                with open(path) as f:
                    j = json.load(f)
                t = (j.get("card") or {}).get(TOKEN_NAME) or j.get(TOKEN_NAME)
                if t:
                    break
            except Exception:
                continue
    return t or None

def planned_restart(name):
    """True when a restart of <name> was requested on purpose in the last 2 minutes (no crash alert)."""
    p = os.path.join(BASE, "logs", f"planned_restart.{name}")
    try:
        return time.time() - os.path.getmtime(p) < 120
    except OSError:
        return False

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "event":
        key = sys.argv[2]
        # the shell scripts pass a key; wording lives here so it stays consistent
        if key.startswith("start:"):
            enqueue_standalone(key, fmt_started())
        elif key.startswith("stop:"):
            enqueue_standalone(key, fmt_stopped())
        elif key.startswith("restart:"):
            name = key.split(":")[1] if ":" in key else "?"
            if not planned_restart(name):
                enqueue_standalone(key, fmt_crash(name))
        elif len(sys.argv) >= 4:
            enqueue_standalone(key, esc(" ".join(sys.argv[3:])))
