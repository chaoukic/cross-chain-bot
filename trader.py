"""Paper trader: ONE fake account shared by all chains. FAKE MONEY ONLY.
Simulates fills against DexScreener prices with per-chain slippage, fees, token tax and a fixed gas cost.
Since 2026-10-03: every reading goes through priceguard.py first, rugs are sold at once ('rug'), and every paper fill
is limited by the pool (constant-product price impact; a sell never gets more than a share of the quote side).
Each fill is stored in `fills`, each position check in `position_ticks`.
Since 2026-10-03 (side-by-side scoring test) there are TWO fake accounts, told apart by positions.scoring:
  'current' = the original account (state keys cash, day, day_start_equity, paused_today, starting_balance - unchanged)
  'new'     = scoring_v1 model score gate (state keys new:cash, new:day, new:day_start_equity, new:paused_today, ...).
They share the candidate stream, guards, sizing rules, exits and fill model, but never each other's cash, caps,
cooldowns or equity, and each fill is modelled as if the other account did not exist.
No wallets, no keys, no orders are ever sent anywhere."""
import threading
from common import (get_state, set_state, decision, now_ts, toronto_date, fnum, log)
from scanner import ds_pairs
import notify, priceguard, newscore

LOCK = threading.Lock()
ACCOUNTS = ("current", "new")

def skey(acct, key):
    """State key of an account: the current account keeps the original global keys."""
    return key if (acct or "current") == "current" else f"{acct}:{key}"

def start_balance(cfg, acct="current"):
    if acct == "new":
        return float((cfg.get("scoring_new") or {}).get("starting_balance_usd", cfg["paper"]["starting_balance_usd"]))
    return float(cfg["paper"]["starting_balance_usd"])

def cash(con, cfg, acct="current"):
    c = get_state(con, skey(acct, "cash"))
    if c is None:
        c = start_balance(cfg, acct)
        set_state(con, skey(acct, "cash"), c)
        set_state(con, skey(acct, "starting_balance"), c)
        if acct != "current":
            set_state(con, skey(acct, "started"), now_ts())
    return c

def set_cash(con, acct, value):
    set_state(con, skey(acct, "cash"), value)

def acct_of(pos):
    try:
        return pos["scoring"] or "current"
    except (KeyError, IndexError):
        return "current"

def chain_cfg(cfg, chain):
    return (cfg.get("chains") or {}).get(chain) or {"slippage_pct": 1.0, "fee_pct": 0.5, "gas_usd": 0}

def _liq_of(pos):
    try:
        return pos["last_liq"] if pos["last_liq"] is not None else pos["entry_liq"]
    except (KeyError, IndexError):
        return None

def sell_detail(cfg, pos, qty, price, liq=None):
    """Modelled paper sell: dict(net, gross, fill_price, capped, impact_pct). gross = min(quote - slippage,
    constant-product proceeds from the pool's liquidity, max share of the quote side); net = after fee, sell tax and gas."""
    C = chain_cfg(cfg, pos["chain"])
    gross, capped = priceguard.sell_gross(cfg, qty, price or 0, liq, C["slippage_pct"])
    net = max(0.0, gross * (1 - C["fee_pct"] / 100) * (1 - (pos["sell_tax_pct"] or 0) / 100) - (pos["gas_usd"] or 0))
    quoted = qty * (price or 0)
    return {"net": net, "gross": gross, "fill_price": gross / qty if qty > 0 else 0.0, "capped": capped,
            "impact_pct": (1 - gross / quoted) * 100 if quoted > 0 else None}

def sell_value(cfg, pos, qty, price, liq=None):
    """What selling qty at price would bring after slippage/pool impact, fee, sell tax and gas."""
    return sell_detail(cfg, pos, qty, price, liq)["net"]

def position_value(cfg, pos):
    return sell_value(cfg, pos, pos["remaining_qty"], pos["last_price"] or 0, _liq_of(pos))

def record_fill(con, pos_id, chain, side, reason, qty, quote_price, liq, fill_price, usd, impact_pct, capped, scoring="current"):
    con.execute("""INSERT INTO fills(pos_id,ts,chain,side,reason,qty,quote_price,liq_usd,fill_price,usd,impact_pct,capped,scoring)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (pos_id, now_ts(), chain, side, reason, qty, quote_price, liq, fill_price, usd, impact_pct, 1 if capped else 0, scoring))

def record_tick(con, cfg, pos_id, chain, price, liq, guard, source, note=None):
    p = con.execute("SELECT * FROM positions WHERE id=?", (pos_id,)).fetchone()
    pnl = None
    if p is not None:
        if p["status"] == "closed":
            pnl = p["pnl_pct"]
        elif p["cost_usd"]:
            pnl = ((p["proceeds_usd"] or 0) + position_value(cfg, p)) / p["cost_usd"] * 100 - 100
    con.execute("INSERT INTO position_ticks(pos_id,ts,chain,price,liq,pnl_pct,guard,source,note,scoring) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (pos_id, now_ts(), chain, price, liq, pnl, guard, source, (note or "")[:300] or None, acct_of(p) if p is not None else "current"))

def equity(con, cfg, acct="current"):
    eq = cash(con, cfg, acct)
    for p in con.execute("SELECT * FROM positions WHERE status='open' AND scoring=?", (acct,)):
        eq += position_value(cfg, p)
    return eq

def day_start_equity(con, acct, default=None):
    return get_state(con, skey(acct, "day_start_equity"), default)

def day_guard(con, cfg, acct="current"):
    """Returns (paused, day_pnl, cap_usd) for one account. Resets its day-start equity at Toronto midnight."""
    today = toronto_date()
    eq = equity(con, cfg, acct)
    k = lambda key: skey(acct, key)
    pre = "" if acct == "current" else f"{acct}:"     # alert keys: the current account keeps its old ones
    lab = None if acct == "current" else acct
    if get_state(con, k("day")) != today:
        if get_state(con, k("paused_today"), False):
            notify.enqueue(con, f"{pre}resume:{today}", notify.fmt_resume(account=acct))
        set_state(con, k("day"), today)
        set_state(con, k("day_start_equity"), eq)
        set_state(con, k("paused_today"), False)
    start = get_state(con, k("day_start_equity"), eq)
    cap = start * cfg["paper"]["daily_loss_cap_pct"] / 100
    pnl = eq - start
    paused = get_state(con, k("paused_today"), False)
    if not paused and pnl <= -cap:
        set_state(con, k("paused_today"), True)
        decision(con, "PAUSE", None, None, None, ("" if acct == "current" else "[NEW SCORING] ")
                 + f"Daily loss cap hit: day P&L ${pnl:,.2f} <= -${cap:,.2f}. New entries paused until midnight Toronto.", scoring=acct)
        notify.enqueue(con, f"{pre}pause:{today}", notify.fmt_pause(pnl, cap, cfg["paper"]["daily_loss_cap_pct"], account=acct))
        paused = True
    return paused, pnl, cap

def _skip(con, c, msg, acct=None):
    """SKIP decision, de-duplicated per token for 30 min (per account when acct is given)."""
    if acct is None:
        hit = con.execute("SELECT 1 FROM decisions WHERE kind='SKIP' AND token=? AND ts>?", (c["token"], now_ts() - 1800)).fetchone()
    else:
        hit = con.execute("SELECT 1 FROM decisions WHERE kind='SKIP' AND token=? AND ts>? AND (scoring=? OR scoring IS NULL)",
                          (c["token"], now_ts() - 1800, acct)).fetchone()
    if not hit:
        decision(con, "SKIP", c["chain"], c["token"], c["symbol"], ("" if acct in (None, "current") else "[NEW SCORING] ") + msg,
                 scoring=acct, score_old=c.get("score"), score_new=c.get("score_new"))

def _gate(c, acct):
    if acct == "new":
        return bool(c.get("gate_new")) and c.get("score_new") is not None
    return c.get("gate_current", True)

def try_entries(con, cfg, candidates):
    """Both accounts, one after the other, from the SAME candidate list. Each account only sees the candidates that
    passed its own score gate, ranks them by its own score and checks only its own cash / caps / cooldown / day cap."""
    with LOCK:
        opened = _try_entries_acct(con, cfg, candidates, "current")
        if newscore.enabled(cfg):
            cash(con, cfg, "new")
            opened += _try_entries_acct(con, cfg, candidates, "new")
        con.commit()
        return opened

def _try_entries_acct(con, cfg, candidates, acct):
    P = cfg["paper"]
    paused, pnl, cap = day_guard(con, cfg, acct)
    opened = 0
    mine = [c for c in candidates if _gate(c, acct)]
    rank = (lambda x: -(x.get("score_new") or 0)) if acct == "new" else (lambda x: -x["score"])
    tag = "" if acct == "current" else "[NEW SCORING] "
    for c in sorted(mine, key=rank):
        sc_txt = f"score {c['score']}" if acct == "current" else f"new score {c.get('score_new')}"
        recent = con.execute("SELECT 1 FROM positions WHERE token=? AND chain=? AND scoring=? AND (status='open' OR closed_at>?)",
                             (c["token"], c["chain"], acct, now_ts() - P["reentry_cooldown_hours"] * 3600)).fetchone()
        if recent:
            continue
        n_open = con.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND scoring=?", (acct,)).fetchone()[0]
        C = chain_cfg(cfg, c["chain"])
        n_chain = con.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND chain=? AND scoring=?", (c["chain"], acct)).fetchone()[0]
        if n_open >= P["max_open_positions"]:
            _skip(con, c, f"passed ({sc_txt}) but max open positions ({P['max_open_positions']}) reached", acct); continue
        if n_chain >= C.get("max_open", P["max_open_positions"]):
            _skip(con, c, f"passed ({sc_txt}) but {c['chain']} already has {n_chain} open (cap {C.get('max_open')})", acct); continue
        if paused:
            _skip(con, c, "passed but daily loss cap is active - no new entries today", acct); continue
        if (get_state(con, "manual_pause") or {}).get("on"):
            _skip(con, c, f"passed ({sc_txt}) but new entries are paused (Telegram /pause)", acct); continue
        eq = equity(con, cfg, acct)
        cs = cash(con, cfg, acct)
        size = min(eq * P["position_pct_of_balance"] / 100, c["liq"] * P["max_pct_of_pool_liquidity"] / 100, cs)
        gas = float(C.get("gas_usd", 0))
        bt, st = c.get("buy_tax") or 0.0, c.get("sell_tax") or 0.0
        if size < 1 + 2 * gas:
            decision(con, "SKIP", c["chain"], c["token"], c["symbol"], f"{tag}size ${size:.2f} too small (cash ${cs:.2f})", scoring=acct,
                     score_old=c.get("score"), score_new=c.get("score_new")); continue
        price = c["price"]
        # fill modelled from the scan reading only: the other account's paper buy never moves this price
        mult = priceguard.buy_multiplier(cfg, size - gas, c["liq"], C["slippage_pct"])   # max(slippage, pool impact)
        fill = price * mult
        qty = (size - gas) * (1 - C["fee_pct"] / 100) * (1 - bt / 100) / fill
        eff = size / qty
        mv = newscore.MODEL_VERSION if c.get("score_new") is not None else None
        con.execute("""INSERT INTO positions(chain,bucket,strategy,token,pair,symbol,url,opened_at,entry_price,entry_eff,qty,remaining_qty,
            cost_usd,peak_price,last_price,last_update,status,score,entry_liq,entry_reason,buy_tax_pct,sell_tax_pct,gas_usd,pnl_usd,pnl_pct,
            last_liq,entry_fill_price,scoring,score_current,score_new,prob_new,model_version)
            VALUES(?,?,'combined',?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?,0,0,?,?,?,?,?,?,?)""",
                    (c["chain"], c["bucket"], c["token"], c["pair"], c["symbol"], c["url"], now_ts(), price, eff, qty, qty, size,
                     price, price, now_ts(), c["score"], c["liq"], c.get("entry_reason"), bt, st, gas, c["liq"], fill,
                     acct, c["score"], c.get("score_new"), c.get("prob_new"), mv))
        pos_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
        record_fill(con, pos_id, c["chain"], "buy", "entry", qty, price, c["liq"], fill, size, (mult - 1) * 100,
                    mult > 1 + C["slippage_pct"] / 100 + 1e-12, scoring=acct)
        set_cash(con, acct, cs - size)
        decision(con, "BUY", c["chain"], c["token"], c["symbol"],
                 f"{tag}PAPER BUY [{c['bucket']}] ${size:,.2f} @ ${price:.10g} (modelled fill ${fill:.10g} = +{(mult - 1) * 100:.2f}% "
                 f"slippage/pool impact; all-in cost ${eff:.10g} incl. {C['fee_pct']}% fee + {bt:.1f}% tax + ${gas:.2f} gas), "
                 f"score {c['score']}" + (f", new score {c['score_new']}" if c.get("score_new") is not None else "")
                 + f", liq ${c['liq']:,.0f}. {c.get('entry_reason') or ''}", scoring=acct,
                 score_old=c.get("score"), score_new=c.get("score_new"))
        notify.enqueue(con, f"{'' if acct == 'current' else acct + ':'}buy:{pos_id}", notify.fmt_buy(
            c["chain"], c["bucket"], c["symbol"], price, size, cfg["buckets"][c["bucket"] or "new"], equity(con, cfg, acct), url=c["url"],
            timing=c.get("timing"), age_min=c.get("age_min"), fallback_why=c.get("entry_reason"),
            account=acct, scores=(c["score"], c.get("score_new"))))
        opened += 1
    return opened

def _pos_scores(pos):
    """Both entry scores of a position for its SELL decision rows (bookkeeping only)."""
    def g(k):
        try:
            return pos[k]
        except (KeyError, IndexError):
            return None
    so = g("score_current")
    return {"score_old": so if so is not None else g("score"), "score_new": g("score_new")}

def _sell(con, cfg, pos, qty, price, reason, liq=None):
    if liq is None:
        liq = _liq_of(pos)
    sd = sell_detail(cfg, pos, qty, price, liq)
    proceeds = sd["net"]
    acct = acct_of(pos)
    tag = "" if acct == "current" else "[NEW SCORING] "
    record_fill(con, pos["id"], pos["chain"], "sell", reason, qty, price, liq, sd["fill_price"], proceeds, sd["impact_pct"], sd["capped"],
                scoring=acct)
    remaining = pos["remaining_qty"] - qty
    total = (pos["proceeds_usd"] or 0) + proceeds
    set_cash(con, acct, cash(con, cfg, acct) + proceeds)
    if remaining <= pos["qty"] * 1e-9:
        pnl = total - pos["cost_usd"]
        con.execute("""UPDATE positions SET remaining_qty=0, proceeds_usd=?, status='closed', closed_at=?, exit_reason=?,
                       pnl_usd=?, pnl_pct=?, last_price=? WHERE id=?""",
                    (total, now_ts(), reason, pnl, pnl / pos["cost_usd"] * 100, price, pos["id"]))
        decision(con, "SELL", pos["chain"], pos["token"], pos["symbol"],
                 f"{tag}PAPER SELL ALL [{pos['bucket']}] @ ${price:.10g} (modelled fill ${sd['fill_price']:.10g}"
                 f"{', capped by pool liquidity' if sd['capped'] else ''}, received ${proceeds:,.2f}): {reason}. "
                 f"Trade P&L ${pnl:+,.2f} ({pnl / pos['cost_usd'] * 100:+.1f}%)", scoring=acct, **_pos_scores(pos))
        eq = equity(con, cfg, acct)
        notify.enqueue(con, f"sell:{pos['id']}:full", notify.fmt_sell(
            pos["chain"], pos["symbol"], pos["entry_price"], price, reason, pnl, pnl / pos["cost_usd"] * 100,
            now_ts() - pos["opened_at"], eq, eq - day_start_equity(con, acct, eq), url=pos["url"],
            after_partial=bool(pos["tp1_done"]), received_usd=proceeds, fill_price=sd["fill_price"], capped=sd["capped"],
            account=acct))
    else:
        con.execute("UPDATE positions SET remaining_qty=?, proceeds_usd=?, tp1_done=1 WHERE id=?", (remaining, total, pos["id"]))
        sold_cost = pos["cost_usd"] * qty / pos["qty"]
        part = proceeds - sold_cost
        decision(con, "SELL", pos["chain"], pos["token"], pos["symbol"],
                 f"{tag}PAPER PARTIAL SELL [{pos['bucket']}] {qty / pos['qty'] * 100:.0f}% @ ${price:.10g} (modelled fill ${sd['fill_price']:.10g}"
                 f"{', capped by pool liquidity' if sd['capped'] else ''}): {reason} (+${proceeds:,.2f}, P&L on part ${part:+,.2f})", scoring=acct,
                 **_pos_scores(pos))
        eq = equity(con, cfg, acct)
        left = dict(pos); left["remaining_qty"] = remaining
        notify.enqueue(con, f"sell:{pos['id']}:partial:{int(pos['tp1_done'] or 0)}", notify.fmt_sell(
            pos["chain"], pos["symbol"], pos["entry_price"], price, reason, part, part / sold_cost * 100,
            now_ts() - pos["opened_at"], eq, eq - day_start_equity(con, acct, eq), partial=True,
            sold_frac=qty / pos["remaining_qty"], left_value=sell_value(cfg, left, remaining, price, liq),
            B=cfg["buckets"][pos["bucket"] or "new"], opened_at=pos["opened_at"], url=pos["url"], account=acct))

def exit_rules(cfg, bucket):
    return cfg["buckets"][bucket or "new"]

def rug_exit(con, cfg, pos, price, liq, why):
    con.execute("UPDATE positions SET last_price=?, last_liq=?, last_update=? WHERE id=?", (price, liq, now_ts(), pos["id"]))
    pos = con.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone()
    return _sell(con, cfg, pos, pos["remaining_qty"], price, f"rug ({why})", liq=liq)

def check_exits(con, cfg, pos, pair, cc=None, source="dexscreener"):
    """Guard the reading, then apply the exit rules. Returns (verdict, raw_price, raw_liq, note) for the tick log."""
    B = exit_rules(cfg, pos["bucket"])
    raw_price, raw_liq = fnum(pair.get("priceUsd")), fnum((pair.get("liquidity") or {}).get("usd"))
    verdict, price, liq, note = priceguard.check_reading(con, cfg, cc or chain_cfg(cfg, pos["chain"]), pos, pair, source)
    if verdict == "reject":
        return verdict, raw_price, raw_liq, note
    if verdict == "rug":
        rug_exit(con, cfg, pos, price, liq, note)
        return verdict, raw_price, raw_liq, note
    peak = max(pos["peak_price"] or price, price)
    con.execute("UPDATE positions SET last_price=?, peak_price=?, last_update=?, last_liq=COALESCE(?, last_liq) WHERE id=?",
                (price, peak, now_ts(), liq, pos["id"]))
    pos = con.execute("SELECT * FROM positions WHERE id=?", (pos["id"],)).fetchone()
    reason = _exit_reason(B, pos, pair, price, peak)
    if reason:
        qty = reason[1] if reason[1] is not None else pos["remaining_qty"]
        _sell(con, cfg, pos, qty, price, reason[0], liq=liq)
        return "sell", raw_price, raw_liq, reason[0]
    return verdict, raw_price, raw_liq, note

def _exit_reason(B, pos, pair, price, peak):
    """(reason, qty or None for everything) of the first exit rule that fires, else None."""
    gain = (price / pos["entry_price"] - 1) * 100
    held_min = (now_ts() - pos["opened_at"]) / 60
    if gain <= -B["stop_loss_pct"]:
        return (f"stop loss ({gain:.1f}% <= -{B['stop_loss_pct']}%)", None)
    if gain >= B["take_profit2_pct"]:
        return (f"take profit 2 (+{gain:.1f}%)", None)
    if gain >= B["take_profit_pct"] and not pos["tp1_done"]:
        frac = B["take_profit_sell_fraction"]
        return (f"take profit 1 (+{gain:.1f}% >= +{B['take_profit_pct']}%)", pos["remaining_qty"] * frac if frac < 1 else None)
    peak_gain = (peak / pos["entry_price"] - 1) * 100
    drop = (1 - price / peak) * 100
    if peak_gain >= B["trailing_activate_pct"] and drop >= B["trailing_stop_pct"]:
        return (f"trailing stop ({drop:.1f}% off peak, peak was +{peak_gain:.1f}%)", None)
    ratio = B.get("volume_decay_ratio", 0)
    if ratio and held_min >= B["volume_decay_min_hold_min"]:
        created = pair.get("pairCreatedAt")
        age_h = (now_ts() - created / 1000) / 3600 if created else 24
        vol = pair.get("volume") or {}
        v1, v6, v24 = fnum(vol.get("h1"), 0), fnum(vol.get("h6"), 0), fnum(vol.get("h24"), 0)
        if age_h >= 12:
            base = v24 * 6 / min(age_h, 24)
            if base > 0 and v6 < ratio * base:
                return (f"volume decay (6h vol ${v6:,.0f} < {ratio*100:.0f}% of avg 6h pace ${base:,.0f})", None)
        elif age_h >= 2:
            base = v6 / min(age_h, 6)
            if base > 0 and v1 < ratio * base:
                return (f"volume decay (1h vol ${v1:,.0f} < {ratio*100:.0f}% of avg hourly pace ${base:,.0f})", None)
    if held_min >= B["max_hold_hours"] * 60:
        return (f"max hold time {B['max_hold_hours']}h", None)
    return None

def update_positions(con, cfg):
    with LOCK:
        opens = con.execute("SELECT * FROM positions WHERE status='open'").fetchall()
        by_chain = {}
        for p in opens:
            by_chain.setdefault(p["chain"], []).append(p)
        for chain, ps in by_chain.items():
            cc = (cfg.get("chains") or {}).get(chain)
            if not cc:
                continue
            con.commit()   # release the write lock before the DexScreener call
            answered = set()
            # one reading per pool for BOTH accounts (a coin held by both is asked for once)
            pairs = ds_pairs(cc, list(dict.fromkeys(p["pair"] for p in ps)), answered)
            for p in ps:
                p = con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
                if p is None or p["status"] != "open":
                    continue
                pr = pairs.get(p["pair"]) or pairs.get((p["pair"] or "").lower())
                try:
                    if pr:
                        if p["guard_missing"]:
                            con.execute("UPDATE positions SET guard_missing=0 WHERE id=?", (p["id"],))
                        verdict, rp, rl, note = check_exits(con, cfg, p, pr, cc)
                        record_tick(con, cfg, p["id"], chain, rp, rl, verdict, "dexscreener", note)
                    elif p["pair"] in answered:
                        # pool missing from an answered batch: removed? (GeckoTerminal confirms after a few misses)
                        what, info = priceguard.pool_missing(con, cfg, cc, p)
                        p = con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
                        if what == "rug":
                            rug_exit(con, cfg, p, info["price"], info["liq"], info["note"])
                            record_tick(con, cfg, p["id"], chain, None, info["liq"], "rug", "geckoterminal", info["note"])
                        elif what == "reading":
                            verdict, rp, rl, note = check_exits(con, cfg, p, info, cc, source="geckoterminal")
                            record_tick(con, cfg, p["id"], chain, rp, rl, verdict, "geckoterminal",
                                        "pool missing on DexScreener; " + (note or ""))
                        else:
                            record_tick(con, cfg, p["id"], chain, None, None, "missing", "dexscreener",
                                        f"pool not in DexScreener answer ({p['guard_missing']} in a row)")
                    else:
                        record_tick(con, cfg, p["id"], chain, None, None, "no_data", "dexscreener",
                                    "DexScreener cool-down / no answer: price not updated")
                except Exception:
                    log.exception("position %s check failed", p["id"])
                con.commit()   # short write per position
        for p in con.execute("SELECT * FROM positions WHERE status='open'").fetchall():
            val = (p["proceeds_usd"] or 0) + position_value(cfg, p)
            con.execute("UPDATE positions SET pnl_usd=?, pnl_pct=? WHERE id=?", (val - p["cost_usd"], (val / p["cost_usd"] - 1) * 100, p["id"]))
        for acct in ACCOUNTS:
            if acct != "current" and get_state(con, skey(acct, "cash")) is None:
                continue   # new account not started (self-test failed / switched off before it ever traded)
            day_guard(con, cfg, acct)
            eq = equity(con, cfg, acct)
            last = con.execute("SELECT ts FROM equity WHERE scoring=? ORDER BY ts DESC LIMIT 1", (acct,)).fetchone()
            if not last or now_ts() - last["ts"] >= 55:
                con.execute("INSERT INTO equity(ts,equity,cash,scoring) VALUES(?,?,?,?)", (now_ts(), eq, cash(con, cfg, acct), acct))
        con.commit()
