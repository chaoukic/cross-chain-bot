"""Offline unit tests (synthetic data, temp DB, no network) for the 2026-10-03 guards. Run:
   .venv/bin/python -m unittest tests/test_guards.py -v"""
import os, sys, tempfile, time, unittest, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common
TMP = tempfile.mkdtemp()
common.DB_PATH = os.path.join(TMP, "test.db")
import tomllib
import priceguard, lpcheck, trader, notify, scanner

CFG = tomllib.load(open(common.CONFIG_PATH, "rb"))
CC = CFG["chains"]["solana"]
EVM = CFG["chains"]["bsc"]
priceguard.gt_pool = lambda *a, **k: None          # never touch the network
priceguard.reject.__globals__["decision"] = common.decision

def pair(price, liq, addr="PAIR1", token="TOK1", mcap=None):
    return {"pairAddress": addr, "baseToken": {"address": token}, "priceUsd": str(price), "liquidity": {"usd": liq},
            "marketCap": mcap, "volume": {"m5": 100, "h1": 1000, "h6": 5000, "h24": 20000},
            "txns": {"m5": {"buys": 3, "sells": 2}}}

class Base(unittest.TestCase):
    def setUp(self):
        common.DB_PATH = os.path.join(TMP, "test.db")
        if os.path.exists(common.DB_PATH):
            os.remove(common.DB_PATH)
        common.init_db()
        self.con = common.db()
        trader.cash(self.con, CFG)
        self.con.commit()
    def tearDown(self):
        self.con.close()
    def open_pos(self, price=1.0, liq=50000.0, chain="solana", size=20.0):
        c = {"chain": chain, "bucket": "new", "token": "TOK1", "symbol": "TST", "pair": "PAIR1", "url": None,
             "price": price, "liq": liq, "score": 60, "entry_reason": "test"}
        notify.enqueue = lambda *a, **k: None
        self.assertEqual(trader.try_entries(self.con, CFG, [c]), 1)
        return self.con.execute("SELECT * FROM positions WHERE status='open'").fetchone()

class TestPriceGuard(Base):
    def test_rejects_10x_spike_and_accepts_normal(self):
        p = self.open_pos()
        v, *_ = priceguard.check_reading(self.con, CFG, CC, p, pair(1.05, 50000))
        self.assertEqual(v, "ok")
        p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
        v, *_ = priceguard.check_reading(self.con, CFG, CC, p, pair(10.0, 50000))
        self.assertEqual(v, "reject")
        # a one-off spike through check_exits must not drive any exit or the peak; the next normal reading is accepted
        p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
        self.con.execute("UPDATE positions SET guard_pending_price=NULL, guard_pending_ts=NULL WHERE id=?", (p["id"],))
        p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
        verdict, *_ = trader.check_exits(self.con, CFG, p, pair(11.0, 50000), CC)
        self.assertEqual(verdict, "reject")
        p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
        self.assertEqual(p["status"], "open"); self.assertLess(p["peak_price"], 2)
        verdict, *_ = trader.check_exits(self.con, CFG, p, pair(1.08, 50000), CC)
        self.assertEqual(verdict, "ok")
        # a spike that persists on the next check WITH trades is accepted (memebot rule) - it is then a real move
        p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
        self.assertEqual(priceguard.check_reading(self.con, CFG, CC, p, pair(9.0, 50000))[0], "reject")
        p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
        self.assertEqual(priceguard.check_reading(self.con, CFG, CC, p, pair(9.5, 50000))[0], "ok")

    def test_rejects_zero_nan_wrong_pair_supply(self):
        p = self.open_pos()
        for bad in (pair(0, 50000), pair(float("nan"), 50000), pair(1.0, 50000, addr="OTHER")):
            self.assertEqual(priceguard.check_reading(self.con, CFG, CC, p, bad)[0], "reject")
        p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
        self.assertEqual(priceguard.check_reading(self.con, CFG, CC, p, pair(1.0, 50000, mcap=1e6))[0], "ok")
        p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
        self.assertEqual(priceguard.check_reading(self.con, CFG, CC, p, pair(1.1, 50000, mcap=5e6))[0], "reject")

    def test_stop_loss_still_works_on_normal_drop(self):
        p = self.open_pos()
        verdict, *_ = trader.check_exits(self.con, CFG, p, pair(0.75, 48000), CC)
        self.assertEqual(verdict, "sell")
        p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
        self.assertTrue(p["exit_reason"].startswith("stop loss"))

    def test_entry_guard(self):
        m = {"price": 1.0, "liq": 50000, "mcap": 1e6, "pair": "PAIR1"}
        ok, _ = priceguard.check_entry(CFG, CC, "solana", "TOKE", m, [pair(1.0, 50000), pair(1.02, 20000, addr="P2")])
        self.assertTrue(ok)
        ok, why = priceguard.check_entry(CFG, CC, "solana", "TOKE", dict(m, price=10.0, mcap=1e7), [])
        self.assertFalse(ok, why)                                   # 10x vs previous scan reading
        ok, why = priceguard.check_entry(CFG, CC, "solana", "TOKF", m, [pair(1.0, 50000), pair(2.0, 20000, addr="P2")])
        self.assertFalse(ok, why)                                   # other pool disagrees
        ok, why = priceguard.check_entry(CFG, CC, "solana", "TOKG", m, [], last_candle=[time.time() - 60, 1, 1, 1, 0.5, 1])
        self.assertFalse(ok, why)                                   # GT candle disagrees
        ok, why = priceguard.check_entry(CFG, CC, "solana", "TOKH", dict(m, price=float("nan")), [])
        self.assertFalse(ok, why)

class TestRug(Base):
    def test_liquidity_drain_confirmed_then_rug_exit(self):
        p = self.open_pos(liq=50000)
        v, *_ = trader.check_exits(self.con, CFG, p, pair(0.3, 5000), CC)
        self.assertEqual(v, "reject")                               # first sighting: wait for confirmation
        p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
        self.assertEqual(p["status"], "open")
        v, *_ = trader.check_exits(self.con, CFG, p, pair(0.25, 4000), CC)
        self.assertEqual(v, "rug")
        p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
        self.assertEqual(p["status"], "closed"); self.assertTrue(p["exit_reason"].startswith("rug ("))

    def test_price_crash_with_liquidity_drop(self):
        p = self.open_pos(liq=50000)
        self.assertEqual(priceguard.check_reading(self.con, CFG, CC, p, pair(0.55, 22000))[0], "ok")  # -45% / -56%: not yet
        p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
        self.assertEqual(priceguard.check_reading(self.con, CFG, CC, p, pair(0.45, 20000))[0], "reject")
        p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
        self.assertEqual(priceguard.check_reading(self.con, CFG, CC, p, pair(0.45, 20000))[0], "rug")

    def test_pool_removed(self):
        p = self.open_pos()
        priceguard.gt_pool = lambda *a, **k: {"missing": True}
        try:
            for i in range(3):
                p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
                what, info = priceguard.pool_missing(self.con, CFG, CC, p)
            self.assertEqual(what, "rug")
            trader.rug_exit(self.con, CFG, p, info["price"], info["liq"], info["note"])
            p = self.con.execute("SELECT * FROM positions WHERE id=?", (p["id"],)).fetchone()
            self.assertEqual(p["status"], "closed"); self.assertAlmostEqual(p["proceeds_usd"], 0.0)
        finally:
            priceguard.gt_pool = lambda *a, **k: None

    def test_rug_alert_text(self):
        t = notify.fmt_sell("solana", "TST", 1.0, 0.2, "rug (pool liquidity down 92% since entry ($50,000 -> $4,000))",
                            -18.5, -92.5, 600, 980, -18.5, received_usd=1.5, fill_price=0.07, capped=True)
        self.assertIn("RUG — TST sold for $1.50", t)

class TestFills(Base):
    def test_large_sell_on_thin_pool_gets_haircut(self):
        pos = {"chain": "solana", "sell_tax_pct": 0, "gas_usd": 0}
        small = trader.sell_detail(CFG, pos, 20, 1.0, 1_000_000)
        self.assertFalse(small["capped"]); self.assertAlmostEqual(small["gross"], 20 * 0.99)
        big = trader.sell_detail(CFG, pos, 5000, 1.0, 4000)        # $5,000 into a $4,000 pool ($2,000 quote side)
        self.assertTrue(big["capped"])
        self.assertLessEqual(big["gross"], 2000 * 0.5 + 1e-9)        # never more than 50% of the quote side
        self.assertLess(big["net"], 5000 * 0.5)
        self.assertAlmostEqual(trader.sell_detail(CFG, pos, 10, 1.0, 0)["net"], 0.0)

    def test_buy_impact_and_fills_rows(self):
        p = self.open_pos(price=1.0, liq=1000, size=20)              # thin pool: impact > 1% slippage
        f = self.con.execute("SELECT * FROM fills WHERE pos_id=? AND side='buy'", (p["id"],)).fetchone()
        self.assertIsNotNone(f); self.assertGreater(f["fill_price"], 1.01)
        self.assertAlmostEqual(p["entry_fill_price"], f["fill_price"])

class TestLpCheck(Base):
    def test_fail_means_no_buy(self):
        lpcheck.rugcheck_report = lambda mint, cache_sec=600: {"markets": [{"pubkey": "PAIR1", "lp": {"lpLockedPct": 10}}], "risks": []}
        st, why = lpcheck.check(CFG, CC, "TOK1", "PAIR1", "new", {})
        self.assertEqual(st, "fail"); self.assertFalse(lpcheck.allows_buy(st, CFG))
        lpcheck.rugcheck_report = lambda mint, cache_sec=600: {"markets": [{"pubkey": "PAIR1", "lp": {"lpLockedPct": 100}}], "risks": []}
        self.assertEqual(lpcheck.check(CFG, CC, "TOK1", "PAIR1", "new", {})[0], "ok")
        lpcheck.rugcheck_report = lambda mint, cache_sec=600: {"markets": [{"pubkey": "PAIR1", "lp": {"lpLockedPct": 100}}],
                                                               "risks": [{"name": "Single holder ownership", "level": "danger"}]}
        self.assertEqual(lpcheck.check(CFG, CC, "TOK1", "PAIR1", "new", {})[0], "fail")
        lpcheck.rugcheck_report = lambda mint, cache_sec=600: None
        st, _ = lpcheck.check(CFG, CC, "TOK1", "PAIR1", "new", {})
        self.assertEqual(st, "unknown"); self.assertFalse(lpcheck.allows_buy(st, CFG))   # fail-closed

    def test_evm_goplus_lp(self):
        r = {"lp_holders": [{"address": "0xabc", "percent": "0.95", "is_locked": 1},
                            {"address": "0x000000000000000000000000000000000000dead", "percent": "0.03", "is_locked": 0}]}
        d = {"lp": lpcheck.evm_lp_summary(r)}
        self.assertEqual(lpcheck.check(CFG, EVM, "0xt", "0xp", "new", d)[0], "ok")
        r = {"lp_holders": [{"address": "0xowner", "percent": "0.9", "is_locked": 0}]}
        self.assertEqual(lpcheck.check(CFG, EVM, "0xt", "0xp", "new", {"lp": lpcheck.evm_lp_summary(r)})[0], "fail")
        self.assertEqual(lpcheck.check(CFG, EVM, "0xt", "0xp", "new", {"lp": None})[0], "unknown")
        self.assertEqual(lpcheck.check(CFG, EVM, "0xt", "0xp", "new", {})[0], "unknown")

class TestTables(Base):
    def test_ticks_and_fills(self):
        p = self.open_pos()
        trader.record_tick(self.con, CFG, p["id"], "solana", 1.01, 50000, "ok", "dexscreener", "")
        trader.record_tick(self.con, CFG, p["id"], "solana", None, None, "no_data", "dexscreener", "cooldown")
        self.con.commit()
        rows = self.con.execute("SELECT * FROM position_ticks WHERE pos_id=? ORDER BY id", (p["id"],)).fetchall()
        self.assertEqual([r["guard"] for r in rows], ["ok", "no_data"]); self.assertIsNotNone(rows[0]["pnl_pct"])
        trader.check_exits(self.con, CFG, p, pair(0.7, 45000), CC)   # stop loss
        self.con.commit()
        s = self.con.execute("SELECT * FROM fills WHERE pos_id=? AND side='sell'", (p["id"],)).fetchone()
        self.assertIsNotNone(s); self.assertLess(s["fill_price"], 0.7)
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(positions)")}
        self.assertTrue({"last_liq", "entry_fill_price", "guard_drain_ts"} <= cols)

if __name__ == "__main__":
    unittest.main()
