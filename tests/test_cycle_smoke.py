"""Offline smoke test of bot.run_cycle's new entry gates (price guard + LP lock) with mocked APIs and a temp DB."""
import os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common
DBP = os.path.join(tempfile.mkdtemp(), "cycle.db")
import bot, scanner, trader, lpcheck, priceguard, notify

def P(price, liq=60000, addr="PAIRX"):
    return {"pairAddress": addr, "baseToken": {"address": "TOKX", "symbol": "TX"}, "priceUsd": str(price),
            "liquidity": {"usd": liq}, "marketCap": 1e6, "pairCreatedAt": (time.time() - 3 * 3600) * 1000, "url": "u",
            "volume": {"h1": 1, "h6": 1, "h24": 1}, "txns": {}, "priceChange": {}}

_ORIG = {"lp": lpcheck.check, "disc": scanner.discover, "hb": scanner.chain_heartbeat, "dst": scanner.ds_tokens,
         "ev": scanner.evaluate, "enq": notify.enqueue, "dsp": trader.ds_pairs}
def _restore():
    lpcheck.check, scanner.discover, scanner.chain_heartbeat = _ORIG["lp"], _ORIG["disc"], _ORIG["hb"]
    scanner.ds_tokens, scanner.evaluate, notify.enqueue, trader.ds_pairs = _ORIG["dst"], _ORIG["ev"], _ORIG["enq"], _ORIG["dsp"]

class T(unittest.TestCase):
    def tearDown(self):
        _restore()
    def run_once(self, lp_status, price=1.0):
        cfg = common.load_config()
        cfg["chains"] = {"solana": cfg["chains"]["solana"]}
        scanner.discover = lambda con, cfg: ([], 0)
        scanner.chain_heartbeat = lambda con, cfg: None
        scanner.ds_tokens = lambda cc, addrs, unavailable=None: {"TOKX": [P(price)]}
        scanner.evaluate = lambda con, cfg, cc, tk, m, budget: ("pass", "timing", ["ok"], 70, False,
                                                               {"safety": {}, "last_candle": [time.time(), 1, 1, 1, 1.0, 1]})
        lpcheck.check = lambda *a, **k: lp_status
        notify.enqueue = lambda *a, **k: None
        bot.run_cycle(cfg)
        con = common.db()
        n = con.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
        ev = con.execute("SELECT stage, result, reasons FROM evaluations ORDER BY id DESC LIMIT 1").fetchone()
        con.close()
        return n, ev

    def test_gates(self):
        common.DB_PATH = DBP + ".gates"
        common.init_db()
        con = common.db(); trader.cash(con, common.load_config())
        con.execute("INSERT INTO tokens(id,chain,address,pool_address,symbol,first_seen,status) VALUES('solana:TOKX','solana','TOKX','PAIRX','TX',?,'watch')", (time.time(),))
        con.commit(); con.close()
        n, ev = self.run_once(("fail", "LP locked 0%"))
        self.assertEqual(n, 0); self.assertEqual(ev["stage"], "lp_lock")
        n, ev = self.run_once(("unknown", "RugCheck unavailable"))
        self.assertEqual(n, 0); self.assertEqual(ev["stage"], "lp_lock"); self.assertEqual(ev["result"], "pending")
        n, ev = self.run_once(("ok", "LP locked 100%"), price=10.0)      # 10x vs previous scan + candle mismatch
        self.assertEqual(n, 0); self.assertEqual(ev["stage"], "price_guard")
        n, ev = self.run_once(("ok", "LP locked 100%"), price=1.0)       # back to normal -> but 0.1x vs last scan: guard
        self.assertEqual(n, 0)
        n, ev = self.run_once(("ok", "LP locked 100%"), price=1.0)       # stable reading -> buy
        self.assertEqual(n, 1, ev["reasons"])
        con = common.db()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM fills WHERE side='buy'").fetchone()[0], 1)
        con.close()

if __name__ == "__main__":
    unittest.main()

class U(unittest.TestCase):
    def tearDown(self):
        _restore()
    def test_update_positions_ticks(self):
        common.DB_PATH = DBP + ".upd"
        common.init_db()
        cfg = common.load_config()
        con = common.db(); trader.cash(con, cfg)
        notify.enqueue = lambda *a, **k: None
        trader.try_entries(con, cfg, [{"chain": "bsc", "bucket": "new", "token": "0xtok", "symbol": "B", "pair": "0xpair",
                                       "url": None, "price": 1.0, "liq": 50000, "score": 60}])
        pid = con.execute("SELECT id FROM positions WHERE status='open' AND chain='bsc'").fetchone()[0]
        good = {"pairAddress": "0xPAIR", "baseToken": {"address": "0xTOK"}, "priceUsd": "1.02", "liquidity": {"usd": 49000}}
        seq = [({}, set()), ({"0xpair": good}, {"0xpair"}), ({}, {"0xpair"})]
        def fake(cc, addrs, answered=None):
            if cc.get("kind") != "evm":
                return {}
            out, ans = seq.pop(0)
            if answered is not None: answered.update(ans)
            return out
        trader.ds_pairs = fake
        for _ in range(3):
            trader.update_positions(con, cfg)
        g = [r[0] for r in con.execute("SELECT guard FROM position_ticks WHERE pos_id=? ORDER BY id", (pid,))]
        self.assertEqual(g[-3:], ["no_data", "ok", "missing"])
        con.close()
