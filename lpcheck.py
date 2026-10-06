"""Liquidity-lock check before every PAPER buy (ported from /workspace/memebot/rugcheck.py, 2026-10-03).
PAPER TRADING ONLY.

* Solana: RugCheck full report (api.rugcheck.xyz, free, no key): LP of the pool we buy in must be >= min_lp_locked_pct
  locked or burned, RugCheck must not mark the coin as rugged, and (reject_danger) no 'danger' risk may be listed.
* BSC / Robinhood: GoPlus `lp_holders` from the token-security answer the safety stage ALREADY fetched (no extra call):
  share of the LP tokens that is locked (is_locked = 1) or sent to a dead/burn address must be >= min_lp_locked_pct.
* Result: ('ok'|'fail'|'unknown', reason). FAIL-CLOSED (as the assessment and memebot): 'unknown' (API unreachable,
  no LP data, e.g. a V3/concentrated pool without LP tokens) never buys; with fail_closed = false it would be allowed."""
import time
from common import http_get, fnum, log

DEFAULTS = {"enabled": True, "min_lp_locked_pct": 90, "reject_danger": True, "fail_closed": True, "cache_sec": 600,
            "rule_new": "pool", "rule_established": "any"}
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{}/report"
EVM_DEAD = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead",
            "0xdead000000000000000042069420694206942069", "0x0000000000000000000000000000000000000001"}
_CACHE = {}

def cfg_lp(cfg):
    d = dict(DEFAULTS); d.update((cfg or {}).get("lpcheck") or {}); return d

def rugcheck_report(mint, cache_sec=600):
    """RugCheck JSON, {'_http404': True} if RugCheck doesn't know the coin, or None if unreachable (cached)."""
    hit = _CACHE.get(mint)
    if hit and time.time() - hit[0] < cache_sec:
        return hit[1]
    j = http_get("rugcheck", RUGCHECK.format(mint), retries=2)
    if j is not None:
        _CACHE[mint] = (time.time(), j)
    if len(_CACHE) > 2000:
        for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[:1000]:
            _CACHE.pop(k, None)
    return j

def evm_lp_summary(goplus_result):
    """From a GoPlus token_security result: {'n': holders, 'locked_pct': locked+burned % of LP supply} or None (no LP data)."""
    hs = (goplus_result or {}).get("lp_holders") or []
    if not hs:
        return None
    locked = burned = 0.0
    for h in hs:
        pct = (fnum(h.get("percent"), 0) or 0) * 100
        addr = (h.get("address") or "").lower()
        if addr in EVM_DEAD or "burn" in (h.get("tag") or "").lower() or "dead" in (h.get("tag") or "").lower():
            burned += pct
        elif str(h.get("is_locked")) == "1":
            locked += pct
    return {"n": len(hs), "locked_pct": round(min(locked + burned, 100.0), 2), "lock_only_pct": round(locked, 2),
            "burned_pct": round(burned, 2), "lp_total_supply": goplus_result.get("lp_total_supply")}

def _solana(cfg, R, token, pair, bucket):
    j = rugcheck_report(token, R["cache_sec"])
    if j is None:
        return "unknown", "RugCheck unavailable (no answer)"
    if j.get("_http404"):
        return "unknown", "RugCheck has no report for this coin yet"
    if j.get("rugged"):
        return "fail", "RugCheck marks this coin as already rugged"
    markets = j.get("markets") or []
    lps = [(m.get("pubkey"), (m.get("lp") or {}).get("lpLockedPct")) for m in markets]
    lps = [(k, float(v)) for k, v in lps if v is not None]
    rule = R["rule_established" if bucket == "established" else "rule_new"]
    mine = [v for k, v in lps if pair and k == pair]
    if rule == "any":
        lp, where = (max(v for _, v in lps), "best pool") if lps else (None, "")
    elif mine:
        lp, where = mine[0], "the pool we buy in"
    else:
        lp, where = (min(v for _, v in lps), "every pool (ours not listed)") if lps else (None, "")
    if lp is None:
        return "unknown", "RugCheck lists no LP-lock figure for this coin's pools"
    if lp < float(R["min_lp_locked_pct"]):
        return "fail", f"pool money not locked (LP locked {lp:.0f}% in {where} < {R['min_lp_locked_pct']}%) - creator could pull it"
    if R["reject_danger"]:
        dangers = sorted({x.get("name", "?") for x in j.get("risks") or [] if x.get("level") == "danger"})
        if dangers:
            return "fail", "RugCheck danger: " + ", ".join(dangers)
    return "ok", f"LP locked {lp:.0f}% ({where}, RugCheck), no danger flags"

def _evm(R, safety):
    if "lp" not in (safety or {}):
        return "unknown", "GoPlus LP data not fetched yet"
    lp = safety.get("lp")
    if not lp:
        return "unknown", "GoPlus has no LP-holder data for this pool (e.g. V3/concentrated pool) - lock cannot be verified"
    if lp["locked_pct"] < float(R["min_lp_locked_pct"]):
        return "fail", (f"pool money not locked (LP locked/burned {lp['locked_pct']:.0f}% < {R['min_lp_locked_pct']}%, GoPlus)"
                        " - creator could pull it")
    return "ok", f"LP locked/burned {lp['locked_pct']:.0f}% (GoPlus: locked {lp['lock_only_pct']:.0f}%, burned {lp['burned_pct']:.0f}%)"

def check(cfg, cc, token, pair, bucket, safety):
    """('ok'|'fail'|'unknown', reason). Never raises."""
    R = cfg_lp(cfg)
    if not R["enabled"]:
        return "ok", "LP-lock check off"
    try:
        return _solana(cfg, R, token, pair, bucket) if cc.get("kind") == "solana" else _evm(R, safety)
    except Exception as e:
        log.warning("lpcheck %s: %s", token, e)
        return "unknown", f"LP-lock check error ({type(e).__name__})"

def allows_buy(status, cfg):
    return status == "ok" or (status == "unknown" and not cfg_lp(cfg)["fail_closed"])
