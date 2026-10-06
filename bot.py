"""Main background process: multi-chain scanner loop + paper-position updater. PAPER TRADING ONLY."""
import json, logging, os, signal, sys, threading, time, traceback
from common import (init_db, db, load_config, apply_limits, now_ts, set_state, get_state, CALLS, log, enabled_chains, ds_stats,
                    decision, backfill_decision_scores)
import scanner, trader, priceguard, lpcheck, newscore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
STOP = threading.Event()

def _bump(d, stage):
    d[stage] = d.get(stage, 0) + 1

HEARTBEAT_SEC = 60          # 2026-10-06: liveness for the admin site (data.json health.heartbeat_ts)
SCORE_LOG_EVERY_SEC = 1800  # 2026-10-06: at most one 'SCORE' decision row per coin per 30 min (no flooding)
_score_logged = {}

def _log_score(con, chain, token, symbol, bucket, score, score_new, extra, res, stage):
    """Bookkeeping only (never changes a gate): one 'SCORE' decision row for a coin that reached the score step,
    with both scores and both gate results. De-duplicated per coin in memory; written in the cycle's open transaction."""
    try:
        t = now_ts()
        if t - _score_logged.get((chain, token), 0) < SCORE_LOG_EVERY_SEC:
            return
        if len(_score_logged) > 5000:
            for k in [k for k, v in _score_logged.items() if t - v >= SCORE_LOG_EVERY_SEC]:
                _score_logged.pop(k, None)
        _score_logged[(chain, token)] = t
        decision(con, "SCORE", chain, token, symbol,
                 f"score step [{bucket}]: current score {score} ({extra.get('gate_current', 'n.a.')}), "
                 f"new score {score_new if score_new is not None else 'n/a'} ({extra.get('gate_new', 'n.a.')}) -> {res} at {stage}",
                 score_old=score, score_new=score_new, quiet=True)
    except Exception as e:
        log.debug("SCORE decision not logged: %s", e)

def run_cycle(cfg):
    con = db()
    started = now_ts()
    calls0 = dict(CALLS)
    prev = get_state(con, "scanner_status", {}) or {}
    set_state(con, "scanner_status", {"state": "scanning", "since": started, "last_run": prev.get("last_run"),
                                      "last_cycle": prev.get("last_cycle"), "duration": prev.get("duration")}); con.commit()
    errors = []
    chains = enabled_chains(cfg)
    all_chains = cfg.get("chains") or {}
    for c, cc in all_chains.items():
        if not cc.get("enabled"):
            log.info("chain %s is DISABLED in config.toml - not scanned", c)
    try:
        scanner.chain_heartbeat(con, cfg); con.commit()
    except Exception as e:
        errors.append(f"heartbeat: {e}")
    found, gt_disc = scanner.discover(con, cfg)
    new = scanner.register(con, found)
    con.commit()
    sc = cfg["scanner"]
    t = now_ts()
    # expire tokens that are neither young nor recently listed (and not held)
    con.execute("""UPDATE tokens SET status='expired' WHERE status='watch' AND first_seen < ? AND COALESCE(last_listed, first_seen) < ?
                   AND id NOT IN (SELECT chain||':'||token FROM positions WHERE status='open')""",
                (t - cfg["buckets"]["new"]["max_age_hours"] * 3600, t - sc.get("established_watch_hours", 48) * 3600))
    con.commit()
    cur = con.execute("INSERT INTO cycles(started) VALUES(?)", (started,))
    cid = cur.lastrowid
    con.commit()
    funnel = {"evaluated": 0, "passed": 0, "rejected": {}, "pending": {}}
    per_chain = {c: {"discovered": sum(1 for f in found if f["chain"] == c), "evaluated": 0, "passed": 0,
                     "by_bucket": {"new": 0, "established": 0}, "deep_by_bucket": {"new": 0, "established": 0},
                     "safety_checked": 0, "rejected": {}, "pending": {}, "watchlist": 0} for c in chains}
    budget = {"safety": int(sc["safety_checks_per_cycle"]), "safety_done": 0, "timing": int(sc["timing_checks_per_cycle"]),
              "rpc_left": int(sc.get("holder_retries_per_cycle", 4))}
    candidates = []
    per_chain_limit = max(30, int(sc["max_watchlist_eval"]) // max(len(chains), 1))
    work = []
    for chain, cc in chains.items():
        per_chain[chain]["watchlist"] = con.execute("SELECT COUNT(*) FROM tokens WHERE status='watch' AND chain=?", (chain,)).fetchone()[0]
        toks = con.execute("""SELECT * FROM tokens WHERE status='watch' AND chain=?
                              ORDER BY CASE WHEN last_stage IN ('safety','holders','holder_trend','score','timing') THEN 0 ELSE 1 END,
                              COALESCE(last_score,0) DESC, COALESCE(last_listed, first_seen) DESC LIMIT ?""", (chain, per_chain_limit)).fetchall()
        unavailable = set()
        pairs = scanner.ds_tokens(cc, [t["address"] for t in toks], unavailable)
        # tokens whose DexScreener batch got no answer (429 cool-down / error) are skipped this cycle:
        # "data unavailable", not "not listed" (they stay on the watchlist untouched and are re-checked next cycle)
        skipped = [tk for tk in toks if tk["address"] in unavailable and not pairs.get(tk["address"])]
        per_chain[chain]["ds_unavailable"] = len(skipped)
        work += [(chain, cc, tk, pairs.get(tk["address"])) for tk in toks if not (tk["address"] in unavailable and not pairs.get(tk["address"]))]
    # deep-stage tokens of all chains first, so they get the per-cycle safety/candle budget
    # and interleave the chains, so one busy chain cannot use up the whole budget
    rank, seen = {}, {}
    for w in work:
        deep = 0 if w[2]["last_stage"] in scanner.DEEP_STAGES else 1
        k = (w[0], deep)
        seen[k] = seen.get(k, 0) + 1
        rank[id(w)] = (deep, seen[k])
    work.sort(key=lambda w: rank[id(w)])
    for chain, cc, tk, tp in work:
        pc = per_chain[chain]
        funnel["evaluated"] += 1; pc["evaluated"] += 1
        m, extra = None, {}
        sn = (None, None)   # new score (scoring_v1) of this evaluation
        bucket = tk["bucket"]
        if not tp:
            final = now_ts() - tk["first_seen"] > 1800
            res, stage, reasons, score = ("reject" if final else "pending"), "data", ["not listed on DexScreener" + (" after 30 min" if final else " yet")], None
            url, sym, pair = None, tk["symbol"], None
        else:
            bp = scanner.best_pair(tp, tk["pool_address"])
            m = scanner.pair_metrics(bp, tp)
            if m["age_min"] is None and tk["pool_created_ts"]:
                m["age_min"] = (now_ts() - tk["pool_created_ts"]) / 60
            bucket = scanner.bucket_of(cfg, m["age_min"])
            # new score from the SAME metrics dict that is saved in evaluations.metrics (+ chain, bucket), at the same moment
            sn = newscore.score(newscore.eval_metrics(m), chain, bucket)
            m["_new"] = sn
            safety_before = budget["safety_done"]
            try:
                res, stage, reasons, score, final, extra = scanner.evaluate(con, cfg, cc, tk, m, budget)
            except Exception as e:
                res, stage, reasons, score, final = "pending", "error", [f"evaluation error: {e}"], None, False
                errors.append(f"{chain} {tk['symbol']}: {e}")
                log.warning("eval error %s %s: %s", chain, tk["symbol"], traceback.format_exc()[-600:])
            pc["safety_checked"] += budget["safety_done"] - safety_before
            if res == "pass":
                # last gates before a buy: price-reading sanity (data already fetched) and the LP-lock check (fail-closed)
                ok, why = priceguard.check_entry(cfg, cc, chain, tk["address"], m, tp, extra.get("last_candle"),
                                                 extra.get("candle_max_age_sec", 1800))
                sym0 = m["symbol"] or tk["symbol"]
                if not ok:
                    priceguard.reject(con, chain, tk["address"], sym0, f"buy blocked: {why}", scores=(score, sn[0]))
                    res, stage, final = "pending", "price_guard", False
                    reasons = [f"price guard: {why} (no buy on this reading)"] + list(reasons)
                else:
                    con.commit()   # never hold the DB write lock across the RugCheck call
                    lst, lwhy = lpcheck.check(cfg, cc, tk["address"], m["pair"], bucket, extra.get("safety"))
                    if not lpcheck.allows_buy(lst, cfg):
                        res, stage, final = ("reject" if lst == "fail" else "pending"), "lp_lock", False
                        reasons = [("LP-lock check failed: " if lst == "fail" else "LP-lock status unknown (fail-closed, no buy): ") + lwhy] + list(reasons)
                        trader._skip(con, {"chain": chain, "token": tk["address"], "symbol": sym0, "score": score, "score_new": sn[0]},
                                     f"passed (score {score}) but LP-lock check {'failed' if lst == 'fail' else 'unknown'}: {lwhy}")
                    else:
                        reasons = list(reasons) + [f"LP-lock check: {lwhy}"]
            else:
                priceguard.note_scan(chain, tk["address"], m.get("price"), m.get("mcap"))
            url, sym, pair = m["url"], m["symbol"] or tk["symbol"], m["pair"]
            if score is not None:   # the rule score is only computed at the score step: this coin reached it
                _log_score(con, chain, tk["address"], sym, bucket, score, sn[0], extra, res, stage)
        if bucket:
            pc["by_bucket"][bucket] = pc["by_bucket"].get(bucket, 0) + 1
            if stage in scanner.DEEP_STAGES:
                pc["deep_by_bucket"][bucket] = pc["deep_by_bucket"].get(bucket, 0) + 1
        g_cur = extra.get("gate_current", "pass") == "pass"    # no gate info (e.g. a mocked evaluate) = current only
        g_new = extra.get("gate_new") == "pass"
        if res == "pass":
            funnel["passed"] += 1; pc["passed"] += 1
            funnel["passed_current"] = funnel.get("passed_current", 0) + (1 if g_cur else 0)
            funnel["passed_new"] = funnel.get("passed_new", 0) + (1 if g_new else 0)
            d = extra.get("safety") or {}
            candidates.append({"chain": chain, "bucket": bucket, "token": tk["address"], "symbol": sym, "pair": pair, "url": url,
                               "price": m["price"], "liq": m["liq"], "score": score, "entry_reason": extra.get("entry_reason"),
                               "buy_tax": d.get("buy_tax"), "sell_tax": d.get("sell_tax"),
                               "timing": extra.get("timing"), "age_min": m.get("age_min"),
                               "gate_current": g_cur, "gate_new": g_new, "score_new": sn[0], "prob_new": sn[1]})
        else:
            key = "rejected" if res == "reject" else "pending"
            _bump(funnel[key], stage); _bump(pc[key], stage)
        metrics = None
        if m:
            metrics = newscore.eval_metrics(m)
            if extra.get("timing"):
                metrics["timing"] = {k: v for k, v in extra["timing"].items() if k != "ma"}
        status = "rejected" if (res == "reject" and final) else "watch"
        con.execute("UPDATE tokens SET last_eval=?, status=?, bucket=?, last_stage=?, last_reasons=?, last_score=?, symbol=COALESCE(?,symbol) WHERE id=?",
                    (now_ts(), status, bucket, stage, "; ".join(reasons), score, sym, tk["id"]))
        con.execute("""INSERT INTO evaluations(cycle_id,ts,chain,bucket,token,symbol,pair,url,result,stage,reasons,score,metrics,
                       score_new,prob_new,model_version,gate_current,gate_new) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cid, now_ts(), chain, bucket, tk["address"], sym, pair, url, res + (" (final)" if status == "rejected" else ""), stage,
                     "; ".join(reasons), score, json.dumps(metrics) if metrics else None,
                     sn[0], sn[1], newscore.MODEL_VERSION if sn[0] is not None else None,
                     extra.get("gate_current", "n.a."), extra.get("gate_new", "n.a.")))
        con.commit()
    opened = trader.try_entries(con, cfg, candidates) if candidates else 0
    calls = {k: CALLS[k] - calls0.get(k, 0) for k in CALLS}
    calls["gt_discovery"] = gt_disc
    con.execute("UPDATE cycles SET finished=?, discovered=?, new_tokens=?, evaluated=?, passed=?, funnel=?, per_chain=?, errors=?, api_calls=? WHERE id=?",
                (now_ts(), len(found), new, funnel["evaluated"], funnel["passed"], json.dumps(funnel), json.dumps(per_chain),
                 json.dumps(errors[:20]), json.dumps(calls), cid))
    set_state(con, "scanner_status", {"state": "idle", "last_run": now_ts(), "last_cycle": cid, "duration": round(now_ts() - started, 1)})
    # evaluations are kept permanently (2026-10-03 side-by-side test: both scores must stay comparable later)
    con.execute("DELETE FROM holder_snaps WHERE ts<?", (now_ts() - 72 * 3600,))
    con.execute("DELETE FROM tokens WHERE status IN ('rejected','expired') AND COALESCE(last_listed, first_seen)<?", (now_ts() - 7 * 86400,))
    con.execute("DELETE FROM cycles WHERE started<?", (now_ts() - 7 * 86400,))
    con.commit()
    con.close()
    log.info("cycle %s: discovered %s (new %s), evaluated %s, passed %s, opened %s | per chain %s | calls %s", cid, len(found), new,
             funnel["evaluated"], funnel["passed"], opened,
             {c: (v["evaluated"], v["by_bucket"], v["passed"]) for c, v in per_chain.items()}, calls)
    st = ds_stats()
    log.info("ds summary: calls last 60s %s, 429s last 60s %s (last 1h %s), cooldown %s, cycle ds calls %s / 429s %s / skipped %s, unavailable tokens %s",
             st["calls_60s"], st["r429_60s"], st["r429_1h"],
             f"ACTIVE {st['cooldown_left']}s left (streak {st['streak']})" if st["cooldown_active"] else "off",
             calls.get("ds", 0), calls.get("ds_429", 0), calls.get("ds_skipped", 0),
             {c: v.get("ds_unavailable", 0) for c, v in per_chain.items()})

def scan_loop():
    while not STOP.is_set():
        t0 = time.time()
        try:
            cfg = load_config(); apply_limits(cfg)
            run_cycle(cfg)
        except Exception:
            log.error("scan cycle failed:\n%s", traceback.format_exc())
            try:
                con = db(); set_state(con, "scanner_status", {"state": "error", "last_error": traceback.format_exc()[-500:], "last_run": now_ts()}); con.commit(); con.close()
            except Exception:
                pass
            cfg = {"scanner": {"scan_interval_sec": 90}}
        STOP.wait(max(5, cfg["scanner"].get("scan_interval_sec", 90) - (time.time() - t0)))

def position_loop():
    while not STOP.is_set():
        interval = 45
        try:
            cfg = load_config(); interval = cfg["paper"].get("update_interval_sec", 45)
            con = db()
            trader.update_positions(con, cfg)
            set_state(con, "trader_heartbeat", now_ts()); con.commit(); con.close()
        except Exception:
            log.error("position update failed:\n%s", traceback.format_exc())
        STOP.wait(interval)

def heartbeat_loop():
    """Writes state 'bot_heartbeat' (unix s) about once a minute from THIS trading process, so the admin site can show
    'offline' when the bot dies (the web process never writes it). One tiny committed write per minute."""
    while not STOP.is_set():
        try:
            con = db()
            try:
                set_state(con, "bot_heartbeat", now_ts()); con.commit()
            finally:
                con.close()
        except Exception as e:
            log.warning("heartbeat write failed (retry in %ss): %s", HEARTBEAT_SEC, e)
        STOP.wait(HEARTBEAT_SEC)

def export_loop():
    """Writes site/data.json (documented in DATA_FORMAT.md) every 30 s for the admin website."""
    import dashboard_data
    while not STOP.is_set():
        try:
            dashboard_data.write_site_json()
        except Exception:
            log.error("site/data.json export failed:\n%s", traceback.format_exc())
        STOP.wait(30)

def main():
    init_db()
    con = db(); cfg = load_config()
    try:   # fold the WAL back into the DB at startup (non-fatal if other readers keep it busy)
        log.info("startup WAL checkpoint (busy, log, checkpointed): %s", tuple(con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()))
    except Exception as e:
        log.warning("startup WAL checkpoint skipped: %s", e)
    trader.cash(con, cfg)
    ok, msg = newscore.selftest()   # side-by-side test: the new account only trades if the copied model re-scores its test rows
    if ok:
        log.info("NEW SCORING self-test PASSED - model %s loaded from %s; new account buys at new score >= %s",
                 newscore.MODEL_VERSION, newscore.MODEL_PATH, newscore.threshold(cfg))
        trader.cash(con, cfg, "new")
    else:
        log.error("!!!!! NEW SCORING SELF-TEST FAILED - the 'new' paper account is DISABLED (current account unaffected): %s", msg)
    set_state(con, "scoring_new_status", {"ok": ok, "msg": msg, "version": newscore.MODEL_VERSION, "ts": now_ts(),
                                          "threshold": newscore.threshold(cfg), "enabled_in_config": newscore.enabled(cfg)})
    set_state(con, "bot_started", now_ts()); set_state(con, "bot_pid", os.getpid()); set_state(con, "bot_heartbeat", now_ts()); con.commit()
    try:   # 2026-10-06: best-effort score_old/score_new for older decision rows (short read pass + one quick write)
        log.info("decision score backfill (rows looked at, filled): %s", backfill_decision_scores(con))
    except Exception as e:
        log.warning("decision score backfill skipped: %s", e)
    con.close()
    signal.signal(signal.SIGTERM, lambda *a: STOP.set())
    for th in (threading.Thread(target=scan_loop, daemon=True), threading.Thread(target=position_loop, daemon=True),
               threading.Thread(target=export_loop, daemon=True), threading.Thread(target=heartbeat_loop, daemon=True)):
        th.start()
    while not STOP.is_set():
        STOP.wait(5)
    log.info("bot stopping")

if __name__ == "__main__":
    main()
