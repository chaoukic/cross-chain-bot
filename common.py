"""Shared helpers: config, SQLite, rate-limited public API clients.
PAPER TRADING ONLY - this code never touches wallets, keys or real trades.
(Patterns copied from /workspace/memebot/common.py; independent copy on purpose.)"""
import json, logging, os, sqlite3, threading, time, tomllib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "crosschain.db")
CONFIG_PATH = os.path.join(BASE, "config.toml")
TZ = ZoneInfo("America/Toronto")
log = logging.getLogger("crosschain")

def load_config():
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)

def enabled_chains(cfg):
    return {k: v for k, v in (cfg.get("chains") or {}).items() if v.get("enabled")}

def now_ts():
    return time.time()

def toronto_date(ts=None):
    return datetime.fromtimestamp(ts if ts is not None else time.time(), TZ).strftime("%Y-%m-%d")

def toronto_midnight_ts(ts=None):
    d = datetime.fromtimestamp(ts if ts is not None else time.time(), TZ)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens(
  id TEXT PRIMARY KEY, chain TEXT, address TEXT, pool_address TEXT, symbol TEXT, dex TEXT,
  pool_created_ts REAL, first_seen REAL, last_listed REAL, last_eval REAL, status TEXT DEFAULT 'watch',
  bucket TEXT, last_stage TEXT, last_reasons TEXT, last_score REAL, source TEXT,
  safety_json TEXT, safety_ts REAL, candles_json TEXT, candles_ts REAL, candles_tf TEXT);
CREATE INDEX IF NOT EXISTS ix_tokens_status ON tokens(status, chain);
CREATE TABLE IF NOT EXISTS cycles(
  id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL, finished REAL, discovered INTEGER,
  new_tokens INTEGER, evaluated INTEGER, passed INTEGER, funnel TEXT, per_chain TEXT, errors TEXT, api_calls TEXT);
CREATE TABLE IF NOT EXISTS evaluations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, cycle_id INTEGER, ts REAL, chain TEXT, bucket TEXT, token TEXT, symbol TEXT,
  pair TEXT, url TEXT, result TEXT, stage TEXT, reasons TEXT, score REAL, metrics TEXT);
CREATE INDEX IF NOT EXISTS ix_eval_ts ON evaluations(ts);
CREATE INDEX IF NOT EXISTS ix_eval_cycle ON evaluations(cycle_id);
CREATE TABLE IF NOT EXISTS holder_snaps(token TEXT, ts REAL, holders INTEGER, price REAL);
CREATE INDEX IF NOT EXISTS ix_hs ON holder_snaps(token, ts);
CREATE TABLE IF NOT EXISTS positions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT, bucket TEXT, strategy TEXT DEFAULT 'combined',
  token TEXT, pair TEXT, symbol TEXT, url TEXT,
  opened_at REAL, entry_price REAL, entry_eff REAL, qty REAL, remaining_qty REAL,
  cost_usd REAL, proceeds_usd REAL DEFAULT 0, peak_price REAL, last_price REAL,
  last_update REAL, status TEXT, closed_at REAL, exit_reason TEXT, tp1_done INTEGER DEFAULT 0,
  score REAL, entry_liq REAL, entry_reason TEXT, buy_tax_pct REAL DEFAULT 0, sell_tax_pct REAL DEFAULT 0,
  gas_usd REAL DEFAULT 0, pnl_usd REAL, pnl_pct REAL);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, chain TEXT, token TEXT, symbol TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS ix_dec_ts ON decisions(ts);
CREATE TABLE IF NOT EXISTS equity(ts REAL, equity REAL, cash REAL);
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS position_ticks(
  id INTEGER PRIMARY KEY AUTOINCREMENT, pos_id INTEGER, ts REAL, chain TEXT, price REAL, liq REAL, pnl_pct REAL,
  guard TEXT, source TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS ix_ticks_pos ON position_ticks(pos_id, ts);
CREATE TABLE IF NOT EXISTS fills(
  id INTEGER PRIMARY KEY AUTOINCREMENT, pos_id INTEGER, ts REAL, chain TEXT, side TEXT, reason TEXT, qty REAL,
  quote_price REAL, liq_usd REAL, fill_price REAL, usd REAL, impact_pct REAL, capped INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_fills_pos ON fills(pos_id);
CREATE TABLE IF NOT EXISTS outbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, text TEXT, created REAL,
  sent_at REAL, attempts INTEGER DEFAULT 0, next_try REAL DEFAULT 0, status TEXT DEFAULT 'pending', error TEXT);
"""

def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=NORMAL")   # safe with WAL; shorter commits
    return con

# columns added to existing tables (2026-10-03: price guard / rug exit / fill model)
MIGRATIONS = {"positions": [("last_liq", "REAL"), ("entry_fill_price", "REAL"), ("guard_last_seen", "REAL"),
                            ("guard_pending_price", "REAL"), ("guard_pending_ts", "REAL"), ("guard_drain_ts", "REAL"),
                            ("guard_supply", "REAL"), ("guard_missing", "INTEGER DEFAULT 0"),
                            # 2026-10-03 side-by-side scoring test: which paper account owns the position + both scores
                            ("scoring", "TEXT NOT NULL DEFAULT 'current'"), ("score_current", "REAL"), ("score_new", "REAL"),
                            ("prob_new", "REAL"), ("model_version", "TEXT")],
              "evaluations": [("score_new", "REAL"), ("prob_new", "REAL"), ("model_version", "TEXT"),
                              ("gate_current", "TEXT"), ("gate_new", "TEXT")],
              "equity": [("scoring", "TEXT NOT NULL DEFAULT 'current'")],
              "fills": [("scoring", "TEXT NOT NULL DEFAULT 'current'")],
              "position_ticks": [("scoring", "TEXT NOT NULL DEFAULT 'current'")],
              "decisions": [("scoring", "TEXT"),    # NULL = not account-specific (scan-side GUARD / LP-lock skips)
                            # 2026-10-06: both scores of the coin when the decision was made (old = current rule score)
                            ("score_old", "REAL"), ("score_new", "REAL")]}
POST_MIGRATION_SQL = """
CREATE INDEX IF NOT EXISTS ix_pos_scoring ON positions(scoring, status);
CREATE INDEX IF NOT EXISTS ix_equity_scoring ON equity(scoring, ts);
UPDATE positions SET score_current=score WHERE score_current IS NULL AND scoring='current';
CREATE INDEX IF NOT EXISTS ix_eval_token ON evaluations(token, ts);
CREATE INDEX IF NOT EXISTS ix_dec_kind ON decisions(kind, id);
"""

def init_db():
    con = db()
    con.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        for name, typ in cols:
            if name not in have:
                try:
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
                except sqlite3.OperationalError as e:   # bot/web/telegram start together: another one added it first
                    if "duplicate column" not in str(e):
                        raise
    con.commit()
    con.executescript(POST_MIGRATION_SQL)
    con.commit()
    con.close()

def get_state(con, key, default=None):
    r = con.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default

def set_state(con, key, value):
    con.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))

def lookup_scores(con, kind, chain, token, ts, scoring=None):
    """Best-effort (score_old, score_new) of a coin around time ts, for decision rows that were not given them.
    old = current rule score, new = scoring_v1 score. BUY/SELL/GUARD look at the paper position first (score_current /
    score_new at entry), everything else at the nearest evaluation (+-6 h) that has a score. Read-only; (None, None) if unknown."""
    if not token:
        return None, None
    def from_pos():
        q = "SELECT COALESCE(score_current, score) so, score_new sn FROM positions WHERE chain IS ? AND token=? AND opened_at<=?"
        a = [chain, token, ts + 5]
        if scoring:
            q += " AND scoring=?"; a.append(scoring)
        return con.execute(q + " ORDER BY opened_at DESC LIMIT 1", a).fetchone()
    def from_eval():
        return con.execute("""SELECT score so, score_new sn FROM evaluations WHERE token=? AND ts BETWEEN ? AND ? AND chain IS ?
                              AND (score IS NOT NULL OR score_new IS NOT NULL) ORDER BY ABS(ts-?) LIMIT 1""",
                           (token, ts - 21600, ts + 21600, chain, ts)).fetchone()
    for f in ((from_pos, from_eval) if kind in ("BUY", "SELL", "GUARD") else (from_eval, from_pos)):
        r = f()
        if r and (r[0] is not None or r[1] is not None):
            return r[0], r[1]
    return None, None

def decision(con, kind, chain, token, symbol, message, scoring=None, score_old=None, score_new=None, quiet=False):
    """scoring = 'current' / 'new' for account-specific decisions (BUY, SELL, SKIP, PAUSE), None for shared ones.
    score_old / score_new (2026-10-06): the coin's current rule score and new (scoring_v1) score; looked up when not given."""
    t = now_ts()
    if score_old is None and score_new is None and token:
        try:
            score_old, score_new = lookup_scores(con, kind, chain, token, t, scoring)
        except Exception as e:   # bookkeeping only: never let it stop a decision row
            log.debug("score lookup for decision failed: %s", e)
    con.execute("INSERT INTO decisions(ts,kind,chain,token,symbol,message,scoring,score_old,score_new) VALUES(?,?,?,?,?,?,?,?,?)",
                (t, kind, chain, token, symbol, message, scoring, score_old, score_new))
    (log.debug if quiet else log.info)("[%s]%s %s %s %s", kind, f"[{scoring}]" if scoring else "", chain or "", symbol or token or "", message)

def backfill_decision_scores(con, limit=5000):
    """One short pass (bot start): fill score_old/score_new of older decision rows from positions/evaluations.
    Reads first, then writes all updates in one quick transaction. Returns (rows looked at, rows filled)."""
    rows = con.execute("""SELECT id, ts, kind, chain, token, scoring FROM decisions
                          WHERE score_old IS NULL AND score_new IS NULL AND token IS NOT NULL ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
    upd = []
    for r in rows:
        so, sn = lookup_scores(con, r["kind"], r["chain"], r["token"], r["ts"], r["scoring"])
        if so is not None or sn is not None:
            upd.append((so, sn, r["id"]))
    if upd:
        con.executemany("UPDATE decisions SET score_old=?, score_new=? WHERE id=? AND score_old IS NULL AND score_new IS NULL", upd)
    con.commit()
    return len(rows), len(upd)

# ---------------------------------------------------------------- rate limiting
class RateLimiter:
    """Thread-safe spacing limiter + cool-down after 429s."""
    def __init__(self, per_min):
        self.lock = threading.Lock()
        self.set_rate(per_min)
        self.next_ok = 0.0
        self.blocked_until = 0.0
    def set_rate(self, per_min):
        self.interval = 60.0 / max(per_min, 0.1)
    def wait(self):
        with self.lock:
            t = time.time()
            start = max(t, self.next_ok, self.blocked_until)
            self.next_ok = start + self.interval
        d = start - time.time()
        if d > 0:
            time.sleep(d)
    def backoff(self, secs):
        with self.lock:
            self.blocked_until = max(self.blocked_until, time.time() + secs)
    def blocked(self):
        return self.blocked_until > time.time()

UA = {"User-Agent": "crosschain-paper-scanner/1.0 (paper trading research)", "Accept": "application/json"}
LIMITS = {"gt": RateLimiter(4), "ds": RateLimiter(30), "goplus": RateLimiter(20), "hp": RateLimiter(20),
          "rpc": RateLimiter(30), "evmrpc": RateLimiter(30),
          "rugcheck": RateLimiter(10)}
CALLS = {k: 0 for k in LIMITS} | {"errors": 0, "gt_429": 0, "ds_429": 0, "ds_skipped": 0}
BACKOFF = {"gt": 120}

class DsGuard:
    """Process-wide DexScreener guard (shared by the scan, position and export threads; telegram/web never call DexScreener).
    - every real request is recorded, so ds_stats() can report calls in the last 60 s;
    - on a 429 a GLOBAL cool-down starts (ds_cooldown_sec, doubling on consecutive 429s up to ds_cooldown_max_sec,
      reset after the next successful call). While it runs no thread sends anything: http_get('ds') returns None at once."""
    def __init__(self):
        self.lock = threading.Lock()
        self.base, self.max = 60.0, 300.0
        self.until = 0.0
        self.streak = 0          # consecutive 429s (cool-downs) without a success in between
        self.sent = []           # timestamps of real requests (last ~2 min)
        self.r429 = []           # timestamps of 429 answers
        self.cooldowns = 0
        self.in_cooldown_logged = False
    def active(self):
        return self.until > time.time()
    def note_call(self):
        with self.lock:
            t = time.time(); self.sent.append(t)
            self.sent = [x for x in self.sent if x > t - 120]
    def note_429(self):
        with self.lock:
            t = time.time(); self.r429.append(t)
            self.r429 = [x for x in self.r429 if x > t - 3600]
            if self.until > t:
                return None              # another thread already started this cool-down
            self.streak += 1
            secs = min(self.base * (2 ** (self.streak - 1)), max(self.max, self.base))
            self.until = t + secs
            self.cooldowns += 1
            self.in_cooldown_logged = True
            return secs, self.streak
    def note_ok(self):
        with self.lock:
            was = self.streak
            self.streak = 0
            ended = self.in_cooldown_logged
            self.in_cooldown_logged = False
            return was if ended else 0
    def stats(self):
        with self.lock:
            t = time.time()
            return {"calls_60s": sum(1 for x in self.sent if x > t - 60), "r429_60s": sum(1 for x in self.r429 if x > t - 60),
                    "r429_1h": len(self.r429), "cooldown_active": self.until > t, "cooldown_left": max(0, round(self.until - t)),
                    "streak": self.streak, "cooldowns": self.cooldowns}

DS_GUARD = DsGuard()

def ds_blocked():
    return DS_GUARD.active()

def ds_stats():
    return DS_GUARD.stats()
_session = requests.Session()
_session.headers.update(UA)

def apply_limits(cfg):
    a = cfg.get("apis", {})
    LIMITS["gt"].set_rate(a.get("geckoterminal_per_min", 4))
    LIMITS["ds"].set_rate(a.get("dexscreener_per_min", 30))
    DS_GUARD.base = float(a.get("ds_cooldown_sec", 60))
    DS_GUARD.max = float(a.get("ds_cooldown_max_sec", 300))
    LIMITS["goplus"].set_rate(a.get("goplus_per_min", 20))
    LIMITS["hp"].set_rate(a.get("honeypot_per_min", 20))
    LIMITS["rpc"].set_rate(a.get("solana_rpc_per_sec", 0.5) * 60)
    BACKOFF["gt"] = a.get("geckoterminal_429_backoff_sec", 120)
    LIMITS["rugcheck"].set_rate((cfg.get("lpcheck") or {}).get("rugcheck_per_min", 10))

def http_get(kind, url, params=None, retries=2):
    lim = LIMITS[kind]
    for attempt in range(retries):
        if kind == "gt" and lim.blocked():
            return None  # never queue up behind a GeckoTerminal cool-down; callers retry next cycle
        if kind == "ds" and DS_GUARD.active():
            CALLS["ds_skipped"] += 1
            return None  # global DexScreener cool-down: data unavailable this cycle, nothing is sent
        lim.wait()       # every attempt (retries included) goes through the shared spacing limiter
        if kind == "ds":
            if DS_GUARD.active():   # a cool-down started while this thread was waiting for its slot
                CALLS["ds_skipped"] += 1
                return None
            DS_GUARD.note_call()
        CALLS[kind] += 1
        try:
            r = _session.get(url, params=params, timeout=20)
        except requests.RequestException as e:
            CALLS["errors"] += 1
            log.warning("%s GET failed %s: %s", kind, url, type(e).__name__)
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code == 429 and kind == "ds":
            CALLS["errors"] += 1
            CALLS["ds_429"] += 1
            started = DS_GUARD.note_429()
            if started:
                log.warning("ds 429 rate limited: GLOBAL DexScreener cool-down %ss (consecutive %s) - no ds calls from any thread until it ends",
                            int(started[0]), started[1])
            return None  # no per-request retry: callers treat the data as unavailable this cycle
        if r.status_code == 429:
            CALLS["errors"] += 1
            wait = BACKOFF["gt"] if kind == "gt" else 15 * (attempt + 1)
            if kind == "gt":
                CALLS["gt_429"] += 1
            log.warning("%s 429 rate limited, backing off %ss", kind, wait)
            lim.backoff(wait)
            if kind == "gt":
                return None
            continue
        if r.status_code >= 500:
            CALLS["errors"] += 1
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 404 and kind in ("hp", "gt", "rugcheck"):
            # honeypot.is "pair not found" / GeckoTerminal "no such pool": a real answer, not an outage
            try:
                body = r.json()
            except ValueError:
                body = {}
            return {"_http404": True, "body": body}
        if r.status_code != 200:
            CALLS["errors"] += 1
            log.warning("%s GET %s -> %s", kind, url, r.status_code)
            return None
        if kind == "ds":
            prev = DS_GUARD.note_ok()
            if prev:
                log.info("ds cool-down over: first DexScreener call OK again (after %s consecutive 429 cool-down(s))", prev)
        try:
            return r.json()
        except ValueError:
            return None
    return None

_rpc_bad_until = {}
def rpc(endpoints, method, params, kind="rpc"):
    """Read-only JSON-RPC against public endpoints, rotating on errors."""
    for ep in endpoints or []:
        if _rpc_bad_until.get((ep, method), 0) > time.time():
            continue
        LIMITS[kind].wait()
        CALLS[kind] += 1
        try:
            r = _session.post(ep, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
            j = r.json()
        except Exception:
            CALLS["errors"] += 1
            _rpc_bad_until[(ep, method)] = time.time() + 30
            continue
        if "error" in j or r.status_code != 200:
            CALLS["errors"] += 1
            _rpc_bad_until[(ep, method)] = time.time() + 20
            continue
        return j.get("result")
    return None

def fnum(x, default=None):
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default
