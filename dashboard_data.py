"""Builds the read-only JSON used by the local dashboard AND written to site/data.json.
The format is documented in DATA_FORMAT.md (keep the two in sync; bump SCHEMA_VERSION on breaking changes)."""
import json, os, tempfile
from datetime import datetime
from common import db, load_config, get_state, now_ts, toronto_midnight_ts, BASE, TZ
import trader, notify, newscore

SCHEMA_VERSION = 1   # side-by-side test (Oct 3) only ADDED keys (accounts, comparison, scoring_test, score_new ...)
STAGE_LABELS = {"data": "Listed on DexScreener", "age": "Pool age window", "liquidity": "Min liquidity",
                "volume": "Volume 1h / 24h", "market_cap": "Market cap range", "txns": "Real buys & sells",
                "safety": "Safety checks (contract / honeypot / tax)", "holders": "Holder count & concentration",
                "holder_trend": "Holders vs price", "score": "Score threshold", "timing": "Good-entry timing",
                "price_guard": "Price-reading check (guard)", "lp_lock": "Pool money locked (LP lock)", "error": "Errors"}
STAGES = ["data", "age", "liquidity", "volume", "market_cap", "txns", "safety", "holders", "holder_trend", "score", "timing",
          "price_guard", "lp_lock", "error"]

OFFLINE_AFTER_SEC = 600      # health block (2026-10-06): the admin site shows "offline" when heartbeat_ts is older than this
DECISIONS_MAX = 100          # decisions lists keep their size limit ...
SCORE_DECISIONS_MAX = 25     # ... and at most this many of them are 'SCORE' rows (2026-10-06), so BUY/SELL/SKIP are not crowded out

def _decisions(con, acct=None):
    """Newest-first decision list (max DECISIONS_MAX, of which max SCORE_DECISIONS_MAX are kind 'SCORE').
    acct=None: all rows (top-level list). acct='current': its own rows + shared/legacy rows (scoring NULL).
    acct='new': its own rows + shared non-trade rows (GUARD / SKIP / SCORE with scoring NULL) since the new account started."""
    if acct is None:
        where, args = "1=1", []
    elif acct == "current":
        where, args = "(scoring='current' OR scoring IS NULL)", []
    else:
        where = "(scoring=? OR (scoring IS NULL AND kind NOT IN ('BUY','SELL','PAUSE') AND ts>=?))"
        args = [acct, get_state(con, trader.skey(acct, "started")) or 0]
    rows = [dict(r) for r in con.execute(f"SELECT * FROM decisions WHERE kind!='SCORE' AND {where} ORDER BY id DESC LIMIT ?",
                                         args + [DECISIONS_MAX])]
    rows += [dict(r) for r in con.execute(f"SELECT * FROM decisions WHERE kind='SCORE' AND {where} ORDER BY id DESC LIMIT ?",
                                          args + [SCORE_DECISIONS_MAX])]
    rows.sort(key=lambda d: -d["id"])
    for d in rows[:DECISIONS_MAX]:
        d.setdefault("score_old", None); d.setdefault("score_new", None)
    return rows[:DECISIONS_MAX]

def _iso(ts):
    return datetime.fromtimestamp(ts, TZ).isoformat(timespec="seconds") if ts else None

def _breakdown(rows, key):
    out = {}
    for p in rows:
        k = p[key] or "unknown"
        b = out.setdefault(k, {"trades": 0, "open": 0, "closed": 0, "wins": 0, "losses": 0, "realized_pnl": 0.0,
                               "unrealized_pnl": 0.0, "invested_open": 0.0, "win_rate": None})
        b["trades"] += 1
        if p["status"] == "open":
            b["open"] += 1; b["unrealized_pnl"] += p["pnl_usd"] or 0; b["invested_open"] += p["cost_usd"] or 0
        else:
            b["closed"] += 1; b["realized_pnl"] += p["pnl_usd"] or 0
            b["wins" if (p["pnl_usd"] or 0) > 0 else "losses"] += 1
    for b in out.values():
        b["win_rate"] = round(b["wins"] / b["closed"] * 100, 1) if b["closed"] else None
        b["total_pnl"] = b["realized_pnl"] + b["unrealized_pnl"]
    return out

def _max_drawdown(points):
    """(max drawdown %, $) of a list of equity values (peak to later trough)."""
    peak, dd, dd_usd = None, 0.0, 0.0
    for v in points:
        if v is None:
            continue
        peak = v if peak is None or v > peak else peak
        if peak and peak > 0:
            if (peak - v) / peak * 100 > dd:
                dd, dd_usd = (peak - v) / peak * 100, peak - v
    return round(dd, 2), round(dd_usd, 2)

def _equity_curve(con, acct, since):
    eqc = [dict(r) for r in con.execute("SELECT ts,equity FROM equity WHERE scoring=? AND ts>? ORDER BY ts", (acct, since))]
    if len(eqc) > 1500:
        eqc = eqc[::len(eqc) // 1500 + 1] + [eqc[-1]]
    return eqc

def account_view(con, cfg, acct, allpos_all, midnight):
    """Everything the dashboard shows for ONE paper account ('current' or 'new')."""
    P = cfg["paper"]
    start_bal = get_state(con, trader.skey(acct, "starting_balance"), trader.start_balance(cfg, acct))
    active = acct == "current" or get_state(con, trader.skey(acct, "cash")) is not None
    eq = trader.equity(con, cfg, acct) if active else start_bal
    cash = get_state(con, trader.skey(acct, "cash"), start_bal)
    allpos = [p for p in allpos_all if (p.get("scoring") or "current") == acct]
    opens = [p for p in allpos if p["status"] == "open"]
    closed_all = [p for p in allpos if p["status"] == "closed"]
    closed = sorted(closed_all, key=lambda p: -(p["closed_at"] or 0))[:300]
    for p in opens:
        _targets(cfg, p)
    n_closed = len(closed_all)
    wins_l = [p["pnl_usd"] or 0 for p in closed_all if (p["pnl_usd"] or 0) > 0]
    loss_l = [p["pnl_usd"] or 0 for p in closed_all if (p["pnl_usd"] or 0) <= 0]
    mp = get_state(con, "manual_pause") or {}
    day_start = trader.day_start_equity(con, acct, eq)
    eqc = _equity_curve(con, acct, now_ts() - 14 * 86400)
    return {
        "scoring": acct, "label": newscore.LABEL[acct], "active": active,
        "account": {"equity": eq, "cash": cash, "start": start_bal, "pnl": eq - start_bal,
                    "pnl_pct": (eq / start_bal - 1) * 100 if start_bal else 0,
                    "closed": n_closed, "wins": len(wins_l), "win_rate": (len(wins_l) / n_closed * 100) if n_closed else None,
                    "losses": len(loss_l), "realized_pnl": sum(wins_l) + sum(loss_l),
                    "expectancy": ((sum(wins_l) + sum(loss_l)) / n_closed) if n_closed else None,
                    "avg_win": (sum(wins_l) / len(wins_l)) if wins_l else None,
                    "avg_loss": (sum(loss_l) / len(loss_l)) if loss_l else None,
                    "open": len(opens), "max_open": P["max_open_positions"],
                    "trades_today": len([p for p in allpos if p["opened_at"] >= midnight]),
                    "day_start_equity": day_start, "day_pnl": eq - day_start, "day_cap": day_start * P["daily_loss_cap_pct"] / 100,
                    "paused_daily_cap": bool(get_state(con, trader.skey(acct, "paused_today"), False)), "manual_pause": bool(mp.get("on")),
                    "started": get_state(con, trader.skey(acct, "started")) if acct != "current" else None},
        "breakdown": {"by_chain": _breakdown(allpos, "chain"), "by_bucket": _breakdown(allpos, "bucket")},
        "open_positions": opens, "closed_trades": closed, "equity_curve": eqc,
        "decisions": _decisions(con, acct),   # 2026-10-06: this account's decisions (+ shared ones), each with score_old / score_new
    }

def comparison(con, accounts, allpos_all):
    """Comparison strip: per account since the side-by-side test started (first use of the new account)."""
    start = get_state(con, trader.skey("new", "started"))
    out = {"test_started": start, "test_started_iso": _iso(start), "accounts": {}}
    if not start:
        return out
    bought = {}
    for acct in accounts:
        ps = [p for p in allpos_all if (p.get("scoring") or "current") == acct and p["opened_at"] >= start]
        cl = [p for p in ps if p["status"] == "closed"]
        w = len([p for p in cl if (p["pnl_usd"] or 0) > 0])
        eqv = [r[0] for r in con.execute("SELECT equity FROM equity WHERE scoring=? AND ts>=? ORDER BY ts", (acct, start))]
        dd, dd_usd = _max_drawdown(eqv)
        bought[acct] = {(p["chain"], p["token"]) for p in ps}
        out["accounts"][acct] = {"label": newscore.LABEL[acct], "trades": len(ps), "open": len(ps) - len(cl), "closed": len(cl),
                                 "wins": w, "win_rate": (w / len(cl) * 100) if cl else None,
                                 "pnl": sum((p["pnl_usd"] or 0) for p in ps), "max_drawdown_pct": dd, "max_drawdown_usd": dd_usd}
    cur, new = bought.get("current", set()), bought.get("new", set())
    out["only_current_coins"], out["only_new_coins"], out["both_coins"] = len(cur - new), len(new - cur), len(cur & new)
    return out

def _targets(cfg, p):
    B = cfg["buckets"].get(p["bucket"] or "new")
    e = p["entry_price"]
    p["targets"] = {"tp1": e * (1 + B["take_profit_pct"] / 100), "tp2": e * (1 + B["take_profit2_pct"] / 100),
                    "sl": e * (1 - B["stop_loss_pct"] / 100),
                    "trail": (p["peak_price"] * (1 - B["trailing_stop_pct"] / 100))
                             if p["peak_price"] and p["peak_price"] >= e * (1 + B["trailing_activate_pct"] / 100) else None,
                    "max_hold_until": p["opened_at"] + B["max_hold_hours"] * 3600}
    p["value_usd"] = (p["proceeds_usd"] or 0) + trader.position_value(cfg, p)

def build():
    cfg = load_config()
    con = db()
    try:
        P = cfg["paper"]
        start_bal = get_state(con, "starting_balance", P["starting_balance_usd"])
        eq, cash = trader.equity(con, cfg), trader.cash(con, cfg)
        midnight = toronto_midnight_ts()
        allpos_all = [dict(r) for r in con.execute("SELECT * FROM positions ORDER BY id")]
        views = {a: account_view(con, cfg, a, allpos_all, midnight) for a in ("current", "new")}
        allpos = [p for p in allpos_all if (p.get("scoring") or "current") == "current"]   # legacy fields = current account
        opens = [p for p in allpos if p["status"] == "open"]
        closed = sorted([p for p in allpos if p["status"] == "closed"], key=lambda p: -(p["closed_at"] or 0))[:300]
        for p in opens:
            B = cfg["buckets"].get(p["bucket"] or "new")
            e = p["entry_price"]
            p["targets"] = {"tp1": e * (1 + B["take_profit_pct"] / 100), "tp2": e * (1 + B["take_profit2_pct"] / 100),
                            "sl": e * (1 - B["stop_loss_pct"] / 100),
                            "trail": (p["peak_price"] * (1 - B["trailing_stop_pct"] / 100))
                                     if p["peak_price"] and p["peak_price"] >= e * (1 + B["trailing_activate_pct"] / 100) else None,
                            "max_hold_until": p["opened_at"] + B["max_hold_hours"] * 3600}
            p["value_usd"] = (p["proceeds_usd"] or 0) + trader.position_value(cfg, p)
        n_closed = len([p for p in allpos if p["status"] == "closed"])
        wins = len([p for p in allpos if p["status"] == "closed" and (p["pnl_usd"] or 0) > 0])
        cycles = [dict(r) for r in con.execute("SELECT * FROM cycles WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 20")]
        for c in cycles:
            for k in ("funnel", "per_chain", "errors", "api_calls"):
                c[k] = json.loads(c[k]) if c[k] else None
        last = cycles[0] if cycles else None
        feed = []
        if last:
            for r in con.execute("""SELECT ts,chain,bucket,token,symbol,url,result,stage,reasons,score,metrics,score_new,prob_new,model_version,
                                    gate_current,gate_new FROM evaluations WHERE cycle_id=?
                                    ORDER BY CASE WHEN result='pass' THEN 0 WHEN stage IN ('timing','score','holder_trend','holders','safety') THEN 1
                                    WHEN result LIKE 'reject%' THEN 2 ELSE 3 END, ts DESC LIMIT 400""", (last["id"],)):
                d = dict(r); d["metrics"] = json.loads(d["metrics"]) if d["metrics"] else None
                feed.append(d)
        deep = []
        for r in con.execute("""SELECT ts,chain,bucket,token,symbol,url,result,stage,reasons,score,metrics,score_new,prob_new,model_version,
                                gate_current,gate_new FROM evaluations
                                WHERE stage IN ('safety','holders','holder_trend','score','timing') AND ts>? ORDER BY ts DESC LIMIT 150""",
                             (now_ts() - 6 * 3600,)):
            d = dict(r); d["metrics"] = json.loads(d["metrics"]) if d["metrics"] else None
            deep.append(d)
        # today's funnel per chain: each token counted once, at its latest result today
        today = {}
        for r in con.execute("""SELECT e.chain, e.bucket, e.result, e.stage FROM evaluations e
                                JOIN (SELECT chain, token, MAX(id) mid FROM evaluations WHERE ts>=? GROUP BY chain, token) x ON e.id = x.mid""", (midnight,)):
            t = today.setdefault(r["chain"], {"tokens": 0, "by_bucket": {}, "passed": 0, "rejected": {}, "pending": {}})
            t["tokens"] += 1
            t["by_bucket"][r["bucket"] or "unknown"] = t["by_bucket"].get(r["bucket"] or "unknown", 0) + 1
            if r["result"] == "pass":
                t["passed"] += 1
            else:
                k = "rejected" if r["result"].startswith("reject") else "pending"
                t[k][r["stage"]] = t[k].get(r["stage"], 0) + 1
        cstat = get_state(con, "chain_status", {}) or {}
        chains = {}
        for name, cc in (cfg.get("chains") or {}).items():
            s = cstat.get(name, {})
            chains[name] = {"label": notify.CHAIN_LABEL.get(name, name), "enabled": bool(cc.get("enabled")), "kind": cc.get("kind"),
                            "chain_id": cc.get("chain_id"), "dexscreener_slug": cc.get("dexscreener_slug"),
                            "geckoterminal_id": cc.get("geckoterminal_id"), "max_open": cc.get("max_open"),
                            "costs": {"slippage_pct": cc.get("slippage_pct"), "fee_pct": cc.get("fee_pct"), "gas_usd": cc.get("gas_usd")},
                            "rpc_ok": s.get("rpc_ok"), "rpc_head": s.get("head"), "rpc_checked": s.get("rpc_checked"),
                            "safety_sources": ["Solana RPC (mint/freeze/top holders)", "GoPlus"] if cc.get("kind") == "solana"
                                              else (["GoPlus"] + (["honeypot.is"] if cc.get("honeypot_is", True) else [])),
                            "watchlist": con.execute("SELECT COUNT(*) FROM tokens WHERE status='watch' AND chain=?", (name,)).fetchone()[0],
                            "last_cycle": (last or {}).get("per_chain", {}).get(name) if last and last.get("per_chain") else None,
                            "today": today.get(name),
                            "note": None if cc.get("enabled") else "disabled in config.toml"}
        eqc = views["current"]["equity_curve"]
        mp = get_state(con, "manual_pause") or {}
        day_start = get_state(con, "day_start_equity", eq)
        ob = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM outbox GROUP BY status")}
        st = get_state(con, "scanner_status", {}) or {}
        return {
            "schema_version": SCHEMA_VERSION,
            "paper_trading_only": True,
            # 2026-10-06 liveness: heartbeat_ts is written ~every 60 s by the trading process (bot.py) only
            "health": {"last_cycle_ts": last["finished"] if last else None, "heartbeat_ts": get_state(con, "bot_heartbeat"),
                       "offline_after_sec": OFFLINE_AFTER_SEC},
            "generated": now_ts(), "generated_iso": _iso(now_ts()), "timezone": "America/Toronto",
            "account": {"equity": eq, "cash": cash, "start": start_bal, "pnl": eq - start_bal,
                        "pnl_pct": (eq / start_bal - 1) * 100 if start_bal else 0,
                        "closed": n_closed, "wins": wins, "win_rate": (wins / n_closed * 100) if n_closed else None,
                        "open": len(opens), "max_open": P["max_open_positions"],
                        "trades_today": len([p for p in allpos if p["opened_at"] >= midnight]),
                        "day_start_equity": day_start, "day_pnl": eq - day_start, "day_cap": day_start * P["daily_loss_cap_pct"] / 100,
                        "paused_daily_cap": bool(get_state(con, "paused_today", False)), "manual_pause": bool(mp.get("on"))},
            "breakdown": {"by_chain": _breakdown(allpos, "chain"), "by_bucket": _breakdown(allpos, "bucket")},
            "chains": chains,
            "scanner": {"state": st.get("state"), "last_run": st.get("last_run"), "last_run_iso": _iso(st.get("last_run")),
                        "duration_sec": st.get("duration"), "last_cycle_id": st.get("last_cycle"), "last_error": st.get("last_error"),
                        "bot_started": get_state(con, "bot_started"), "trader_heartbeat": get_state(con, "trader_heartbeat"),
                        "interval_sec": cfg["scanner"]["scan_interval_sec"]},
            "funnel": {"stages": STAGES, "stage_labels": STAGE_LABELS, "last_cycle": last and {k: last[k] for k in
                       ("id", "started", "finished", "discovered", "new_tokens", "evaluated", "passed", "funnel", "per_chain", "api_calls", "errors")}},
            "open_positions": opens, "closed_trades": closed,
            "feed": feed, "deep": deep,
            "decisions": _decisions(con),   # each item carries score_old / score_new (2026-10-06)
            "equity_curve": eqc,
            "cycles": [{k: c[k] for k in ("id", "started", "finished", "discovered", "new_tokens", "evaluated", "passed", "api_calls")} for c in cycles],
            "telegram": {"status": (get_state(con, "tg_status") or {}).get("state"), "outbox": ob},
            "strategy": {"name": "combined", "label": "One combined strategy (safety + score + good-entry timing)",
                         "buckets": {"new": f"pool younger than {cfg['buckets']['new']['max_age_hours']}h",
                                     "established": f"{cfg['buckets']['new']['max_age_hours']}h to {cfg['buckets']['established']['max_age_days']} days old"}},
            "config": cfg,
            # ---- side-by-side scoring test (Oct 3): both paper accounts; the legacy fields above = 'current'
            "accounts": views,
            "comparison": comparison(con, ("current", "new"), allpos_all),
            "scoring_test": {"model_version": newscore.MODEL_VERSION, "threshold_new": newscore.threshold(cfg),
                             "threshold_current": {b: cfg["buckets"][b]["min_score_to_buy"] for b in ("new", "established")},
                             "selftest": get_state(con, "scoring_new_status") or {"ok": newscore.STATUS["ok"], "msg": newscore.STATUS["msg"]},
                             "enabled": bool((cfg.get("scoring_new") or {}).get("enabled", True)),
                             "tabs": {"current": "#current", "new": "#new"}},
        }
    finally:
        con.close()

def write_site_json(path=None):
    path = path or os.path.join(BASE, "site", "data.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = build()
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, default=str, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    # also keep a static copy of the dashboard next to data.json (it falls back to data.json when there is no server)
    import shutil
    site = os.path.dirname(path)
    os.makedirs(os.path.join(site, "static"), exist_ok=True)
    shutil.copyfile(os.path.join(BASE, "static", "index.html"), os.path.join(site, "index.html"))
    for n in ("style.css", "app.js", "chart.umd.min.js"):
        shutil.copyfile(os.path.join(BASE, "static", n), os.path.join(site, "static", n))
    return path

if __name__ == "__main__":
    print(write_site_json())
