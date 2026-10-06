#!/usr/bin/env python3
"""Reference scorer for scoring_v1 (standard library only).

    from score_v1 import score
    s, p = score("crosschain", metrics)   # metrics = the bot's evaluation metrics dict (+ "chain", "bucket" for Cross Chain)
    # s = 0-100 (probability x 100, 1 decimal), p = probability of "+20% before -25% within 60 min"

Models: models/<bot>_v1.json (bot = "memebot" for Trading Bot, "crosschain" for Cross Chain).
Self-test: python3 score_v1.py   (re-scores the 10 stored rows in models/<bot>_v1_testrows.json and
compares with the probabilities scikit-learn produced when the model was fitted).
"""
import json, math, os

HERE = os.path.dirname(os.path.abspath(__file__))
_MODELS = {}

FEATURE_DOC = {
    "log_liq": "log10(metrics['liq']) - pool liquidity USD (DexScreener liquidity.usd); missing if liq is None or <= 0",
    "log_vol_h1": "ln(1 + metrics['vol_h1']) - 1h volume USD; missing if None or < 0",
    "log_vol_h24": "ln(1 + metrics['vol_h24']) - 24h volume USD; missing if None or < 0",
    "vol_liq": "slog(vol_h1 / liq); missing if vol_h1 is None or liq is None or <= 0",
    "vol_h1_share": "vol_h1 / vol_h24 (share of the day's volume done in the last hour); missing if vol_h24 is None or <= 0",
    "log_mcap": "log10(metrics['mcap']); missing if None or <= 0",
    "mcap_liq": "slog(mcap / liq); missing if mcap or liq is None or <= 0",
    "log_age_min": "ln(1 + metrics['age_min']) - pool age in minutes; missing if None or < 0",
    "log_buys_h1": "ln(1 + metrics['buys_h1']); missing if None or < 0",
    "log_sells_h1": "ln(1 + metrics['sells_h1']); missing if None or < 0",
    "bs_ratio": "slog(buys_h1 / max(sells_h1, 1)); missing if buys_h1 or sells_h1 is None",
    "chg_h1": "slog(metrics['chg_h1']) - 1h price change %; missing if None",
    "chg_m5": "slog(metrics['chg_m5']) - 5m price change %; missing if None",
    "chg_h24": "slog(metrics['chg_h24']) - 24h price change %; missing if None",
    "is_bsc": "1.0 if metrics['chain'] == 'bsc' else 0.0",
    "is_robinhood": "1.0 if metrics['chain'] == 'robinhood' else 0.0",
    "is_established": "1.0 if metrics['bucket'] == 'established' else 0.0 (Cross Chain bucket_of())",
}
# slog(x) = sign(x) * ln(1 + |x|)

def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None

def _slog(x):
    return None if x is None else math.copysign(math.log1p(abs(x)), x)

def _log1p_nonneg(x):
    return math.log1p(x) if x is not None and x >= 0 else None

def raw_features(m):
    g = lambda k: _num(m.get(k))
    liq, v1, v24, mc, age = g("liq"), g("vol_h1"), g("vol_h24"), g("mcap"), g("age_min")
    b, s = g("buys_h1"), g("sells_h1")
    return {
        "log_liq": math.log10(liq) if liq is not None and liq > 0 else None,
        "log_vol_h1": _log1p_nonneg(v1),
        "log_vol_h24": _log1p_nonneg(v24),
        "vol_liq": _slog(v1 / liq) if v1 is not None and liq is not None and liq > 0 else None,
        "vol_h1_share": v1 / v24 if v1 is not None and v24 is not None and v24 > 0 else None,
        "log_mcap": math.log10(mc) if mc is not None and mc > 0 else None,
        "mcap_liq": _slog(mc / liq) if mc is not None and mc > 0 and liq is not None and liq > 0 else None,
        "log_age_min": _log1p_nonneg(age),
        "log_buys_h1": _log1p_nonneg(b),
        "log_sells_h1": _log1p_nonneg(s),
        "bs_ratio": _slog(b / max(s, 1.0)) if b is not None and s is not None else None,
        "chg_h1": _slog(g("chg_h1")),
        "chg_m5": _slog(g("chg_m5")),
        "chg_h24": _slog(g("chg_h24")),
        "is_bsc": 1.0 if m.get("chain") == "bsc" else 0.0,
        "is_robinhood": 1.0 if m.get("chain") == "robinhood" else 0.0,
        "is_established": 1.0 if m.get("bucket") == "established" else 0.0,
    }

def load(bot, path=None):
    if bot not in _MODELS or path:
        with open(path or os.path.join(HERE, "models", f"{bot}_v1.json")) as fh:
            _MODELS[bot] = json.load(fh)
    return _MODELS[bot]

def out_of_domain(m):
    liq, mc = _num(m.get("liq")), _num(m.get("mcap"))
    return liq is not None and mc is not None and liq > 0 and mc > 0 and mc / liq < 1.3

def score(bot, metrics, model=None):
    """-> (score 0-100, probability). Out-of-domain (liquidity ~= market cap) pools return (0.0, 0.0)."""
    M = model or load(bot)
    if out_of_domain(metrics):
        return 0.0, 0.0
    raw = raw_features(metrics)
    z = M["intercept"]
    for f in M["features"]:
        x = raw.get(f["name"])
        if x is not None:
            x = min(max(x, f["clip_low"]), f["clip_high"])
        else:
            x = f["fill_missing"]
        z += f["coef"] * (x - f["mean"]) / f["std"]
    p = 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))
    return round(p * 100, 1), p

def _selftest():
    ok = True
    for bot in ("crosschain", "memebot"):
        rows = json.load(open(os.path.join(HERE, "models", f"{bot}_v1_testrows.json")))
        worst = 0.0
        for r in rows:
            s, p = score(bot, r["metrics"])
            worst = max(worst, abs(p - r["expected_prob"]))
        good = worst < 1e-9
        ok &= good
        print(f"{bot}: {len(rows)} stored rows, max |prob - fitted prob| = {worst:.2e} -> {'PASS' if good else 'FAIL'}; "
              f"threshold {load(bot)['recommended_buy_threshold']}")
    return ok

if __name__ == "__main__":
    raise SystemExit(0 if _selftest() else 1)
