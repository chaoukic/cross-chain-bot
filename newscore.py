"""'New scoring' (scoring_v1) for the side-by-side paper test. PAPER TRADING ONLY.
Uses the COPIES in scoring_v1/ (score_v1.py + models/crosschain_v1.json), never the analysis-bot originals, so a
later re-fit there cannot silently change a running test.
- selftest(): re-scores the 10 stored rows (models/crosschain_v1_testrows.json); if it fails the 'new' account stays off.
- score(metrics, chain, bucket) -> (score 0-100, prob). metrics = the same dict that is saved in evaluations.metrics.
The new score is a probability x 100 and is NOT on the same scale as the current 0-100 rule score."""
import importlib.util, json, logging, os

BASE = os.path.dirname(os.path.abspath(__file__))
DIR = os.path.join(BASE, "scoring_v1")
MODEL_PATH = os.path.join(DIR, "models", "crosschain_v1.json")
ROWS_PATH = os.path.join(DIR, "models", "crosschain_v1_testrows.json")
ACCOUNTS = ("current", "new")
LABEL = {"current": "Current scoring", "new": "New scoring"}
log = logging.getLogger("crosschain")

_spec = importlib.util.spec_from_file_location("crosschain_score_v1", os.path.join(DIR, "score_v1.py"))
score_v1 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(score_v1)
MODEL = score_v1.load("crosschain", MODEL_PATH)
MODEL_VERSION = f"{MODEL.get('model', 'scoring_v1')} {MODEL.get('version', '?')}"   # "scoring_v1 1.0"
THRESHOLD = float(MODEL["recommended_buy_threshold"])                                 # 38.4

STATUS = {"ran": False, "ok": False, "msg": "self-test not run yet", "version": MODEL_VERSION}

# keys of the scan metrics that are saved in evaluations.metrics (and fed to the scorer)
METRIC_KEYS = ("price", "liq", "vol_h1", "vol_h24", "mcap", "age_min", "buys_h1", "sells_h1", "chg_m5", "chg_h1", "chg_h24")

def eval_metrics(m):
    """The evaluations.metrics dict built from scanner.pair_metrics output (same rounding as before)."""
    return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in m.items() if k in METRIC_KEYS}

def selftest():
    """Re-score the stored rows with the in-bot copy; PASS only if every probability matches within 1e-9."""
    try:
        with open(ROWS_PATH) as f:
            rows = json.load(f)
        worst = 0.0
        for r in rows:
            _, p = score_v1.score("crosschain", r["metrics"], model=MODEL)
            worst = max(worst, abs(p - r["expected_prob"]))
        ok = len(rows) >= 10 and worst < 1e-9
        msg = f"{MODEL_VERSION}: {len(rows)} stored rows, max |prob - fitted prob| = {worst:.2e} -> {'PASS' if ok else 'FAIL'}; threshold {THRESHOLD}"
    except Exception as e:
        ok, msg = False, f"{MODEL_VERSION}: self-test crashed: {type(e).__name__}: {e} -> FAIL"
    STATUS.update(ran=True, ok=ok, msg=msg)
    return ok, msg

def enabled(cfg=None):
    """True when the 'new' account may trade: config switch on AND the self-test passed (run lazily once)."""
    if cfg is not None and not ((cfg.get("scoring_new") or {}).get("enabled", True)):
        return False
    if not STATUS["ran"]:
        selftest()
    return STATUS["ok"]

def threshold(cfg=None):
    return float(((cfg or {}).get("scoring_new") or {}).get("min_score_to_buy", THRESHOLD))

def score(metrics, chain, bucket):
    """(score_0_100, prob) for an evaluations.metrics dict, or (None, None) while the new scoring is off."""
    if not enabled():
        return None, None
    d = dict(metrics or {}); d["chain"] = chain; d["bucket"] = bucket
    return score_v1.score("crosschain", d, model=MODEL)

if __name__ == "__main__":
    ok, msg = selftest()
    print(msg)
    raise SystemExit(0 if ok else 1)
