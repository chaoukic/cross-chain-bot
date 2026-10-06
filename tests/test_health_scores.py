"""Offline tests (temp DB, no network) for the 2026-10-06 additions: data.json `health` block, bot heartbeat,
score_old/score_new on decisions, SCORE decision rows (capped in data.json) and the venv self-heal check.
   .venv/bin/python -m unittest tests/test_health_scores.py -v"""
import os, subprocess, sys, tempfile, threading, time, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import common
TMP = tempfile.mkdtemp()
common.DB_PATH = os.path.join(TMP, "health.db")
import bot, trader, notify, dashboard_data

CFG0 = common.load_config()

def fresh_db(name):
    common.DB_PATH = os.path.join(TMP, name)
    common.init_db()
    con = common.db()
    trader.cash(con, CFG0); trader.cash(con, CFG0, "new"); con.commit()
    return con

def cand(token, score=60, score_new=45.0, g_cur=True, g_new=True):
    return {"chain": "solana", "bucket": "new", "token": token, "symbol": token[:4], "pair": "P" + token, "url": None,
            "price": 1.0, "liq": 50000.0, "score": score, "entry_reason": "test", "gate_current": g_cur, "gate_new": g_new,
            "score_new": score_new, "prob_new": score_new / 100}

class T(unittest.TestCase):
    def setUp(self):
        self._enq = notify.enqueue
        notify.enqueue = lambda *a, **k: None
    def tearDown(self):
        notify.enqueue = self._enq

    def test_health_block(self):
        con = fresh_db("h1.db")
        d = dashboard_data.build()
        self.assertEqual(d["health"], {"last_cycle_ts": None, "heartbeat_ts": None, "offline_after_sec": 600})
        con.execute("INSERT INTO cycles(started, finished) VALUES(?, ?)", (100.0, 123.5))
        common.set_state(con, "bot_heartbeat", 456.0); con.commit(); con.close()
        d = dashboard_data.build()
        self.assertEqual(d["health"], {"last_cycle_ts": 123.5, "heartbeat_ts": 456.0, "offline_after_sec": 600})

    def test_heartbeat_thread_writes_state(self):
        fresh_db("h2.db").close()
        bot.STOP.clear()
        th = threading.Thread(target=bot.heartbeat_loop, daemon=True); th.start()
        t0 = time.time()
        while time.time() - t0 < 5:
            con = common.db(); hb = common.get_state(con, "bot_heartbeat"); con.close()
            if hb:
                break
            time.sleep(0.05)
        bot.STOP.set(); th.join(5); bot.STOP.clear()
        self.assertIsNotNone(hb); self.assertLess(abs(hb - time.time()), 10)

    def test_decision_scores_everywhere(self):
        con = fresh_db("h3.db")
        trader.try_entries(con, CFG0, [cand("AAA1", score=61.5, score_new=47.2)])
        rows = con.execute("SELECT kind, scoring, score_old, score_new FROM decisions WHERE kind='BUY' ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in rows], [("BUY", "current", 61.5, 47.2), ("BUY", "new", 61.5, 47.2)])
        pos = con.execute("SELECT * FROM positions WHERE scoring='current'").fetchone()
        trader._sell(con, CFG0, pos, pos["remaining_qty"], 1.1, "test exit"); con.commit()
        s = con.execute("SELECT score_old, score_new FROM decisions WHERE kind='SELL'").fetchone()
        self.assertEqual(tuple(s), (61.5, 47.2))
        con.close()
        d = dashboard_data.build()
        lists = [d["decisions"], d["accounts"]["current"]["decisions"], d["accounts"]["new"]["decisions"]]
        for lst in lists:
            self.assertTrue(lst)
            for x in lst:
                self.assertIn("score_old", x); self.assertIn("score_new", x)
        self.assertTrue(all(x["scoring"] in ("current", None) for x in d["accounts"]["current"]["decisions"]))
        self.assertTrue(all(x["scoring"] in ("new", None) for x in d["accounts"]["new"]["decisions"]))

    def test_lookup_and_backfill(self):
        con = fresh_db("h4.db")
        t = time.time()
        con.execute("INSERT INTO evaluations(ts,chain,token,result,stage,score,score_new) VALUES(?,?,?,?,?,?,?)",
                    (t - 100, "bsc", "0xold", "reject", "score", 55.0, 30.1))
        con.execute("INSERT INTO decisions(ts,kind,chain,token,symbol,message) VALUES(?,?,?,?,?,?)", (t - 90, "SKIP", "bsc", "0xold", "O", "old row"))
        con.execute("INSERT INTO decisions(ts,kind,chain,token,symbol,message) VALUES(?,?,?,?,?,?)", (t - 90, "SKIP", "bsc", "0xnone", "N", "no data"))
        con.commit()
        self.assertEqual(common.backfill_decision_scores(con), (2, 1))
        r = con.execute("SELECT score_old, score_new FROM decisions WHERE token='0xold'").fetchone()
        self.assertEqual(tuple(r), (55.0, 30.1))
        common.decision(con, "GUARD", "bsc", "0xold", "O", "[PRICE-GUARD] test")      # no scores given -> looked up
        r = con.execute("SELECT score_old, score_new FROM decisions WHERE kind='GUARD'").fetchone()
        self.assertEqual(tuple(r), (55.0, 30.1))
        con.close()

    def test_score_rows_deduped_and_capped(self):
        con = fresh_db("h5.db")
        bot._score_logged.clear()
        ex = {"gate_current": "reject", "gate_new": "pass"}
        bot._log_score(con, "solana", "TOKS", "S", "new", 40.0, 50.0, ex, "reject", "score")
        bot._log_score(con, "solana", "TOKS", "S", "new", 41.0, 51.0, ex, "reject", "score")   # same coin within 30 min: skipped
        con.commit()
        r = con.execute("SELECT kind, scoring, score_old, score_new FROM decisions").fetchall()
        self.assertEqual([tuple(x) for x in r], [("SCORE", None, 40.0, 50.0)])
        for i in range(80):
            common.decision(con, "SCORE", "solana", f"T{i}", "x", "score step", score_old=i, score_new=i, quiet=True)
        for i in range(5):
            common.decision(con, "SKIP", "solana", f"K{i}", "k", "skip", scoring="current", score_old=1.0, score_new=2.0)
        con.commit(); con.close()
        d = dashboard_data.build()
        dec = d["decisions"]
        self.assertLessEqual(len(dec), dashboard_data.DECISIONS_MAX)
        self.assertEqual(sum(1 for x in dec if x["kind"] == "SCORE"), dashboard_data.SCORE_DECISIONS_MAX)
        self.assertEqual(sum(1 for x in dec if x["kind"] == "SKIP"), 5)
        self.assertEqual([x["id"] for x in dec], sorted([x["id"] for x in dec], reverse=True))

    def test_venv_check_function(self):
        sh = os.path.join(HERE, "venv_check.sh")
        ok = subprocess.run(["bash", "-c", f'. "{sh}"; venv_ok "{os.path.join(HERE, ".venv")}"'], capture_output=True)
        self.assertEqual(ok.returncode, 0)
        bad = subprocess.run(["bash", "-c", f'. "{sh}"; venv_ok "{os.path.join(TMP, "no_venv_here")}"'], capture_output=True)
        self.assertNotEqual(bad.returncode, 0)

if __name__ == "__main__":
    unittest.main()
