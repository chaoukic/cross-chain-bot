"""Offline tests (temp DB, mocked APIs, no network) for the 2026-10-03 side-by-side scoring test.
   .venv/bin/python -m unittest tests/test_sidebyside.py -v"""
import importlib.util, json, os, sqlite3, sys, tempfile, time, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import common
TMP = tempfile.mkdtemp()
common.DB_PATH = os.path.join(TMP, "sbs.db")
import bot, scanner, trader, lpcheck, priceguard, notify, newscore

REF_PATH = "/home/box/analysis-bot/scoring/score_v1.py"      # reference scorer (read-only: no bytecode written there)
sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location("ref_score_v1", REF_PATH)
REF = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(REF)
ROWS = json.load(open(newscore.ROWS_PATH))
CFG0 = common.load_config()
_ORIG = {"lp": lpcheck.check, "disc": scanner.discover, "hb": scanner.chain_heartbeat, "dst": scanner.ds_tokens,
         "ev": scanner.evaluate, "enq": notify.enqueue, "dsp": trader.ds_pairs}

def restore():
    lpcheck.check, scanner.discover, scanner.chain_heartbeat = _ORIG["lp"], _ORIG["disc"], _ORIG["hb"]
    scanner.ds_tokens, scanner.evaluate, notify.enqueue, trader.ds_pairs = _ORIG["dst"], _ORIG["ev"], _ORIG["enq"], _ORIG["dsp"]

def fresh_db(name):
    common.DB_PATH = os.path.join(TMP, name)
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(common.DB_PATH + ext):
            os.remove(common.DB_PATH + ext)
    common.init_db()
    con = common.db()
    trader.cash(con, CFG0); trader.cash(con, CFG0, "new"); con.commit()
    return con

def cand(token="TOK1", chain="solana", g_cur=True, g_new=False, score=60, score_new=40.0, liq=50000.0, price=1.0):
    return {"chain": chain, "bucket": "new", "token": token, "symbol": token[:4], "pair": "P" + token, "url": None,
            "price": price, "liq": liq, "score": score, "entry_reason": "test", "gate_current": g_cur, "gate_new": g_new,
            "score_new": score_new, "prob_new": score_new / 100 if score_new is not None else None}

# a real stored row (scoring_v1 test row #5: new score 52.1), as a DexScreener pair
ROW5 = dict(ROWS[4]["metrics"])
def ds_pair(chg_m5=None):
    m = ROW5
    return {"pairAddress": "PAIRQ", "url": "u", "dexId": "raydium", "baseToken": {"address": "TOKQ", "symbol": "TQ"},
            "priceUsd": str(m["price"]), "liquidity": {"usd": m["liq"]}, "marketCap": m["mcap"],
            "volume": {"h1": m["vol_h1"], "h6": m["vol_h24"], "h24": m["vol_h24"]},
            "txns": {"m5": {"buys": 50, "sells": 40}, "h1": {"buys": int(m["buys_h1"]), "sells": int(m["sells_h1"])},
                     "h24": {"buys": int(m["buys_h1"]), "sells": int(m["sells_h1"])}},
            "priceChange": {"m5": m["chg_m5"] if chg_m5 is None else chg_m5, "h1": m["chg_h1"], "h6": m["chg_h1"], "h24": m["chg_h24"]},
            "pairCreatedAt": (time.time() - m["age_min"] * 60) * 1000}
SAFE = {"ts": time.time(), "src": ["solana_rpc", "goplus"], "kind": "solana", "mint_authority": None, "freeze_authority": None,
        "mint_known": True, "supply_raw": 10**15, "holders": 500, "top1_pct": 4.6, "top10_pct": 30, "holders_src": "solana_rpc"}

def add_token(con, safety=SAFE):
    con.execute("""INSERT INTO tokens(id,chain,address,pool_address,symbol,first_seen,status,safety_json,safety_ts)
                   VALUES('solana:TOKQ','solana','TOKQ','PAIRQ','TQ',?,'watch',?,?)""", (time.time(), json.dumps(safety), time.time()))
    con.commit()


class TestScorer(unittest.TestCase):
    def test_selftest_and_version(self):
        ok, msg = newscore.selftest()
        self.assertTrue(ok, msg)
        self.assertEqual(newscore.MODEL_VERSION, "scoring_v1 1.0")
        self.assertEqual(newscore.THRESHOLD, 38.4)
        self.assertEqual(newscore.threshold(CFG0), 38.4)

    def test_matches_reference_on_test_rows(self):
        for r in ROWS:
            m = {k: v for k, v in r["metrics"].items() if k not in ("chain", "bucket")}
            got = newscore.score(m, r["metrics"]["chain"], r["metrics"]["bucket"])
            ref = REF.score("crosschain", r["metrics"])
            self.assertEqual(got[0], ref[0]); self.assertAlmostEqual(got[1], ref[1], places=12)
            self.assertAlmostEqual(got[1], r["expected_prob"], places=9)

    def test_matches_reference_on_real_evaluations(self):
        """Live crosschain.db rows (read-only): in-bot score of the stored metrics == reference score_v1.py."""
        src = os.path.join(HERE, "crosschain.db")
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT chain, bucket, metrics FROM evaluations WHERE metrics IS NOT NULL ORDER BY id DESC LIMIT 300").fetchall()
        finally:
            con.close()
        self.assertGreater(len(rows), 20)
        n_ood = 0
        for chain, bucket, mj in rows:
            m = json.loads(mj)
            got = newscore.score(m, chain, bucket)
            ref = REF.score("crosschain", dict(m, chain=chain, bucket=bucket))
            self.assertEqual(got, ref)
            n_ood += got == (0.0, 0.0)
        print(f"\n  {len(rows)} real evaluation rows: in-bot == reference on all ({n_ood} out-of-domain -> 0)")

    def test_domain_guard(self):
        self.assertEqual(newscore.score({"liq": 100000, "mcap": 110000, "vol_h1": 5e5}, "solana", "new"), (0.0, 0.0))


class TestAccounts(unittest.TestCase):
    def setUp(self):
        notify.enqueue = lambda *a, **k: None
    def tearDown(self):
        restore()

    def test_new_only_pass_buys_only_in_new(self):
        con = fresh_db("acct1.db")
        cur0, new0 = trader.cash(con, CFG0), trader.cash(con, CFG0, "new")
        self.assertEqual(new0, 1000.0)
        n = trader.try_entries(con, CFG0, [cand(g_cur=False, g_new=True, score=40, score_new=45.0)])
        self.assertEqual(n, 1)
        rows = con.execute("SELECT scoring, score_current, score_new, prob_new, model_version FROM positions").fetchall()
        self.assertEqual([r[0] for r in rows], ["new"])
        self.assertEqual(tuple(rows[0])[1:], (40, 45.0, 0.45, "scoring_v1 1.0"))
        self.assertEqual(trader.cash(con, CFG0), cur0)                       # current cash untouched
        self.assertAlmostEqual(trader.cash(con, CFG0, "new"), new0 - 20.0)   # 2% of the new account's own equity
        self.assertEqual(con.execute("SELECT scoring FROM fills").fetchone()[0], "new")
        self.assertEqual(con.execute("SELECT scoring FROM decisions WHERE kind='BUY'").fetchone()[0], "new")
        con.close()

    def test_both_pass_both_buy_same_fill(self):
        con = fresh_db("acct2.db")
        self.assertEqual(trader.try_entries(con, CFG0, [cand(g_cur=True, g_new=True)]), 2)
        f = con.execute("SELECT scoring, fill_price, usd FROM fills ORDER BY scoring").fetchall()
        self.assertEqual([r[0] for r in f], ["current", "new"])
        self.assertAlmostEqual(f[0][1], f[1][1])     # neither paper buy moved the other's price
        con.close()

    def test_caps_cash_cooldown_independent(self):
        con = fresh_db("acct3.db")
        P = CFG0["paper"]
        # fill the CURRENT account to its total cap (spread over chains so per-chain caps are not the limit)
        chains = ["solana", "solana", "solana", "bsc", "bsc"]
        for i, ch in enumerate(chains[:P["max_open_positions"]]):
            trader.try_entries(con, CFG0, [cand(token=f"C{i}", chain=ch, g_cur=True, g_new=False)])
        n_cur = con.execute("SELECT COUNT(*) FROM positions WHERE scoring='current' AND status='open'").fetchone()[0]
        self.assertEqual(n_cur, P["max_open_positions"])
        # current is full -> a both-pass coin is bought only by new
        self.assertEqual(trader.try_entries(con, CFG0, [cand(token="X1", chain="robinhood", g_cur=True, g_new=True)]), 1)
        self.assertEqual(con.execute("SELECT scoring FROM positions WHERE token='X1'").fetchall()[0][0], "new")
        # cooldown is per account: current never held Y1, new did (closed) -> new skips, current (if room) would buy
        con.execute("UPDATE positions SET status='closed', closed_at=? WHERE scoring='current' AND token='C0'", (time.time(),))
        trader.set_cash(con, "new", trader.cash(con, CFG0, "new"))
        self.assertEqual(trader.try_entries(con, CFG0, [cand(token="X1", chain="robinhood", g_cur=True, g_new=True)]), 1)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM positions WHERE token='X1'").fetchone()[0], 2)  # current bought, new on cooldown
        # per-chain cap is per account: new has only 1 robinhood position, current has its own
        con.close()

    def test_daily_cap_per_account(self):
        con = fresh_db("acct4.db")
        trader.day_guard(con, CFG0, "new")
        common.set_state(con, "new:paused_today", True)
        self.assertEqual(trader.try_entries(con, CFG0, [cand(g_cur=True, g_new=True)]), 1)
        self.assertEqual([r[0] for r in con.execute("SELECT scoring FROM positions")], ["current"])
        # a loss in the new account trips only the new account's cap
        con = fresh_db("acct4b.db")
        trader.day_guard(con, CFG0, "current"); trader.day_guard(con, CFG0, "new")
        trader.set_cash(con, "new", 900.0)          # -10% on the new account only
        self.assertTrue(trader.day_guard(con, CFG0, "new")[0])
        self.assertFalse(trader.day_guard(con, CFG0, "current")[0])
        self.assertTrue(common.get_state(con, "new:paused_today")); self.assertFalse(common.get_state(con, "paused_today"))
        con.close()

    def test_manual_pause_blocks_both(self):
        con = fresh_db("acct5.db")
        common.set_state(con, "manual_pause", {"on": True})
        self.assertEqual(trader.try_entries(con, CFG0, [cand(g_cur=True, g_new=True)]), 0)
        con.close()

    def test_exits_same_pass_same_reading(self):
        con = fresh_db("acct6.db")
        trader.try_entries(con, CFG0, [cand(token="Z", chain="bsc", g_cur=True, g_new=True)])
        calls = []
        def fake(cc, addrs, answered=None):
            calls.append(list(addrs))
            if cc.get("kind") != "evm":
                return {}
            if answered is not None: answered.update(addrs)
            pr = {"pairAddress": "PZ", "baseToken": {"address": "Z"}, "priceUsd": "0.7", "liquidity": {"usd": 49000}}
            return {"PZ": pr, "pz": pr}
        trader.ds_pairs = fake
        trader.update_positions(con, CFG0)
        self.assertIn(["PZ"], calls)                 # one reading for the pool, not one per account
        st = {r[0]: (r[1], r[2]) for r in con.execute("SELECT scoring, status, exit_reason FROM positions")}
        self.assertEqual(st["current"][0], "closed"); self.assertEqual(st["new"][0], "closed")   # -30% stop loss on both
        self.assertEqual(st["current"][1], st["new"][1])
        eq = {r[0] for r in con.execute("SELECT scoring FROM equity")}
        self.assertEqual(eq, {"current", "new"})
        ticks = {r[0] for r in con.execute("SELECT scoring FROM position_ticks")}
        self.assertEqual(ticks, {"current", "new"})
        con.close()


class TestGateAndCycle(unittest.TestCase):
    def setUp(self):
        notify.enqueue = lambda *a, **k: None
    def tearDown(self):
        restore()

    def _eval(self, con, chg_m5=None, timing_budget=2):
        tk = con.execute("SELECT * FROM tokens WHERE id='solana:TOKQ'").fetchone()
        p = ds_pair(chg_m5)
        m = scanner.pair_metrics(p, [p])
        budget = {"safety": 0, "safety_done": 0, "timing": timing_budget, "rpc_left": 0}
        return scanner.evaluate(con, CFG0, CFG0["chains"]["solana"], tk, m, budget), m

    def test_new_only_pass_still_hits_timing_gate(self):
        con = fresh_db("gate1.db"); add_token(con)
        (res, stage, reasons, sc, final, extra), m = self._eval(con)       # stored chg_m5 -14.8%: "dropping fast"
        sn = newscore.score(newscore.eval_metrics(m), "solana", "new")[0]
        self.assertLess(sc, CFG0["buckets"]["new"]["min_score_to_buy"], reasons)
        self.assertGreaterEqual(sn, 38.4)
        self.assertEqual((extra["gate_current"], extra["gate_new"]), ("reject", "pass"))
        self.assertEqual((res, stage), ("reject", "timing"), reasons)       # NOT a pass: the timing gate applied
        self.assertIn("dropping fast", reasons[0])
        con.close()

    def test_both_fail_rejects_at_score(self):
        con = fresh_db("gate2.db"); add_token(con, dict(SAFE, top1_pct=4.9, holders=None))
        p = ds_pair(); p["volume"].update(h1=6000, h24=3e6); p["txns"]["h1"] = {"buys": 30, "sells": 29}   # weak new score too
        p["pairCreatedAt"] = (time.time() - 20 * 3600) * 1000
        tk = con.execute("SELECT * FROM tokens WHERE id='solana:TOKQ'").fetchone()
        m = scanner.pair_metrics(p, [p])
        res, stage, reasons, sc, final, extra = scanner.evaluate(con, CFG0, CFG0["chains"]["solana"], tk, m,
                                                                 {"safety": 0, "safety_done": 0, "timing": 2, "rpc_left": 0})
        self.assertEqual((res, stage), ("reject", "score"), reasons)
        self.assertEqual((extra["gate_current"], extra["gate_new"]), ("reject", "reject"))
        con.close()

    def test_gt_budget_out_defers_for_both_accounts(self):
        con = fresh_db("gate3.db"); add_token(con, dict(SAFE, top1_pct=1.0))   # current score passes too
        cfg = json.loads(json.dumps(CFG0)); cfg["chains"] = {"solana": CFG0["chains"]["solana"]}
        cfg["scanner"]["timing_checks_per_cycle"] = 0                             # GeckoTerminal budget used up
        scanner.discover = lambda con, cfg: ([], 0)
        scanner.chain_heartbeat = lambda con, cfg: None
        scanner.ds_tokens = lambda cc, addrs, unavailable=None: {"TOKQ": [ds_pair(chg_m5=2.0)]}
        lpcheck.check = lambda *a, **k: ("ok", "LP locked 100%")
        bot.run_cycle(cfg)
        ev = con.execute("SELECT result, stage, gate_current, gate_new, score, score_new, prob_new, model_version, metrics FROM evaluations").fetchone()
        self.assertEqual((ev["result"], ev["stage"]), ("pending", "timing"))
        self.assertEqual((ev["gate_current"], ev["gate_new"]), ("pass", "pass"))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM positions").fetchone()[0], 0)   # neither account bought
        # the logged new score is exactly the reference score of the logged metrics dict
        ref = REF.score("crosschain", dict(json.loads(ev["metrics"]), chain="solana", bucket="new"))
        self.assertEqual((ev["score_new"], ev["prob_new"]), ref); self.assertEqual(ev["model_version"], "scoring_v1 1.0")
        con.close()

    def test_cycle_routes_new_only_pass_and_keeps_evaluations(self):
        con = fresh_db("cycle1.db")
        con.execute("INSERT INTO tokens(id,chain,address,pool_address,symbol,first_seen,status) VALUES('solana:TOKX','solana','TOKX','PAIRX','TX',?,'watch')", (time.time(),))
        old = time.time() - 40 * 3600
        con.execute("INSERT INTO evaluations(ts,chain,token,result,stage) VALUES(?,?,?,?,?)", (old, "solana", "OLD", "reject", "liquidity"))
        con.execute("INSERT INTO holder_snaps(token,ts,holders,price) VALUES('x',?,1,1)", (time.time() - 80 * 3600,))
        con.commit()
        cfg = json.loads(json.dumps(CFG0)); cfg["chains"] = {"solana": CFG0["chains"]["solana"]}
        pair = {"pairAddress": "PAIRX", "baseToken": {"address": "TOKX", "symbol": "TX"}, "priceUsd": "1.0", "liquidity": {"usd": 60000},
                "marketCap": 1e6, "pairCreatedAt": (time.time() - 3 * 3600) * 1000, "url": "u", "volume": {"h1": 1, "h6": 1, "h24": 1},
                "txns": {}, "priceChange": {}}
        scanner.discover = lambda con, cfg: ([], 0)
        scanner.chain_heartbeat = lambda con, cfg: None
        scanner.ds_tokens = lambda cc, addrs, unavailable=None: {"TOKX": [pair]}
        scanner.evaluate = lambda con, cfg, cc, tk, m, budget: ("pass", "timing", ["ok"], 40, False,
                                                               {"safety": {}, "last_candle": [time.time(), 1, 1, 1, 1.0, 1],
                                                                "gate_current": "reject", "gate_new": "pass"})
        lpcheck.check = lambda *a, **k: ("ok", "LP locked 100%")
        bot.run_cycle(cfg)          # first reading: entry price guard has no previous scan -> ok
        pos = con.execute("SELECT scoring FROM positions").fetchall()
        self.assertEqual([r[0] for r in pos], ["new"])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM evaluations WHERE token='OLD'").fetchone()[0], 1)   # kept (no 36h delete)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM holder_snaps").fetchone()[0], 0)                   # other housekeeping as before
        e = con.execute("SELECT gate_current, gate_new, score_new FROM evaluations WHERE token='TOKX'").fetchone()
        self.assertEqual((e[0], e[1]), ("reject", "pass")); self.assertIsNotNone(e[2])
        con.close()


class TestAlertsAndDashboard(unittest.TestCase):
    def test_alert_labels(self):
        # Telegram labels were renamed on purpose (notify.py, Oct 3: "OLD/NEW scoring trade", "Scores: old · new"); 2026-10-06 test updated to match
        B = CFG0["buckets"]["new"]
        b = notify.fmt_buy("solana", "new", "AB<C", 1.0, 20, B, 1000, account="new", scores=(41.2, 45.3))
        self.assertTrue(b.startswith("🏷 <b>NEW scoring</b> trade\n🟢 <b>BOUGHT AB&lt;C</b>"))
        self.assertIn("<b>Scores:</b> old 41.2 · new 45.3", b)
        s = notify.fmt_sell("bsc", "X", 1, 1.2, "take profit 2 (+20%)", 4, 20, 3600, 1004, 4, account="current")
        self.assertTrue(s.startswith("🏷 <b>OLD scoring</b> trade\n"))
        self.assertIn("NEW scoring</b> account", notify.fmt_pause(-60, 50, 5, account="new"))
        self.assertNotIn("scoring", notify.fmt_buy("solana", "new", "A", 1.0, 20, B, 1000))   # old callers unchanged

    def test_dashboard_and_telegram_both_accounts(self):
        con = fresh_db("dash.db")
        notify.enqueue = lambda *a, **k: None
        trader.try_entries(con, CFG0, [cand(token="D1", g_cur=True, g_new=True), cand(token="D2", chain="bsc", g_cur=False, g_new=True)])
        con.close()
        import dashboard_data, telegram_bot
        d = dashboard_data.build()
        self.assertEqual(set(d["accounts"]), {"current", "new"})
        self.assertEqual(len(d["accounts"]["new"]["open_positions"]), 2)
        self.assertEqual(len(d["open_positions"]), 1)                    # legacy field = current account only
        self.assertEqual(d["account"]["equity"], d["accounts"]["current"]["account"]["equity"])
        self.assertEqual(d["comparison"]["only_new_coins"], 1); self.assertEqual(d["comparison"]["both_coins"], 1)
        for k in ("schema_version", "account", "breakdown", "chains", "scanner", "funnel", "open_positions", "closed_trades",
                  "feed", "deep", "decisions", "equity_curve", "cycles", "telegram", "strategy", "config"):
            self.assertIn(k, d)
        con = common.db()
        st = telegram_bot.status_text(con, CFG0); ps = telegram_bot.positions_text(con, CFG0)
        self.assertIn("Current scoring", st); self.assertIn("New scoring", st)
        self.assertIn("Current scoring</b> (1 open)", ps); self.assertIn("New scoring</b> (2 open)", ps)
        con.close()
        restore()

if __name__ == "__main__":
    unittest.main()
