"""Price guard, rug detection and pool-capped paper fills (ported from /workspace/memebot/priceguard.py, 2026-10-03).
PAPER TRADING ONLY - fake money, no wallets, no keys, no orders.

* Entry guard  (check_entry):  a scan reading that is zero/NaN, jumps implausibly vs the previous scan, disagrees with the
  token's other pools or with the GeckoTerminal candles already fetched, or implies a different supply -> no buy.
* Position guard (check_reading): every DexScreener reading of an open position is checked before any exit rule sees it.
  Verdicts: 'ok' (act on it), 'reject' (ignore this reading: no buy, no sell), 'rug' (pool drained / crashed, confirmed on
  two checks -> sell everything at once, fill capped by what the pool can still pay).
* Fills (sell_gross / buy_multiplier): constant-product price impact from the pool's current liquidity, and a sell never
  receives more than a fraction of the quote side of the pool.
Every rejected reading is logged with a [PRICE-GUARD] tag (decisions table kind 'GUARD', de-duplicated per 10 min).
No extra DexScreener calls are made: only data the bot already fetched in its batched calls is used. GeckoTerminal is
asked (rarely) only to confirm an extreme reading or a pool that disappeared, and only when its limiter has a free slot."""
import math, time
from common import http_get, fnum, now_ts, decision, log, LIMITS

GT = "https://api.geckoterminal.com/api/v2"

GUARD_DEFAULTS = {
    "enabled": True,
    "max_jump_up_factor": 5.0,       # reading > 5x the last good price = extreme (needs confirmation)
    "max_drop_pct": 50,              # reading > 50% below the last good price = extreme (needs confirmation)
    "confirm_tolerance_pct": 25,     # second source / next check must agree within this
    "confirm_window_sec": 300,       # a pending extreme reading must be confirmed within this
    "stale_gap_min": 10,             # after a data gap this long the first reading is not acted on
    "max_supply_deviation_pct": 50,  # market cap / price must imply the same supply (+/- this)
    "use_second_source": True,       # ask GeckoTerminal to confirm extreme readings (only if a GT slot is free)
    "pair_mismatch_pct": 30,         # entry: price vs the median of the token's other pools (>= min_ref_pair_liq_usd)
    "min_ref_pair_liq_usd": 5000,    # entry: other pools below this liquidity are not used as a reference
    "candle_mismatch_pct": 30,       # entry: price vs the latest GeckoTerminal candle close already fetched
    "entry_jump_window_sec": 900,    # entry: compare with the previous scan reading if it is younger than this
}
RUG_DEFAULTS = {
    "enabled": True,
    "liq_drain_pct": 80,             # pool liquidity down this much from entry = drained (rug)
    "min_exit_liquidity_usd": 1000,  # pool liquidity below this = drained (rug)
    "crash_liq_drop_pct": 50,        # OR liquidity down this much from entry ...
    "crash_price_drop_pct": 50,      # ... together with price down this much from entry = rug
    "pool_missing_checks": 3,        # pool missing from answered DexScreener batches this many checks in a row -> ask GT
}
FILL_DEFAULTS = {
    "fill_cap": True,                # constant-product price impact on every paper buy and sell
    "max_fill_pct_of_quote_liq": 50, # a sell never receives more than this % of the pool's quote side (liquidity / 2)
}

def _merge(defaults, cfg, key):
    d = dict(defaults); d.update((cfg or {}).get(key) or {}); return d

def cfg_guard(cfg): return _merge(GUARD_DEFAULTS, cfg, "guard")
def cfg_rug(cfg): return _merge(RUG_DEFAULTS, cfg, "rug")
def cfg_fills(cfg): return _merge(FILL_DEFAULTS, cfg, "fills")

def good(x):
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x) and x > 0

def _get(row, k, default=None):
    try:
        v = row[k]
    except (KeyError, IndexError):
        return default
    return default if v is None else v

_last_logged = {}
def reject(con, chain, token, symbol, msg, log_decision=True, scores=None):
    """scores = optional (score_old, score_new) for the decision row (2026-10-06, bookkeeping only)."""
    log.warning("[PRICE-GUARD] %s %s: %s", chain, symbol, msg)
    key = (chain, token, msg.split(":")[0][:40])
    if con is not None and log_decision and time.time() - _last_logged.get(key, 0) > 600:
        _last_logged[key] = time.time()
        so, sn = scores or (None, None)
        decision(con, "GUARD", chain, token, symbol, "[PRICE-GUARD] " + msg, score_old=so, score_new=sn)

# ---------------------------------------------------------------- fills
def cp_sell_usd(qty, price, liq):
    """What a constant-product pool with total liquidity `liq` (USD, both sides) pays for qty tokens."""
    v = qty * price
    if liq is None:
        return v
    r = max(liq, 0) / 2
    return r * v / (r + v) if r > 0 and v > 0 else 0.0

def sell_gross(cfg, qty, price, liq, slippage_pct):
    """Gross USD for a paper sell BEFORE fee / tax / gas: (gross, capped).
    min(quoted price - slippage, constant-product proceeds, max_fill_pct_of_quote_liq of the quote side)."""
    F = cfg_fills(cfg)
    if not good(price) or qty <= 0:
        return 0.0, liq is not None
    quoted = qty * price * (1 - slippage_pct / 100)
    if not F["fill_cap"] or liq is None:
        return quoted, False
    cap = min(cp_sell_usd(qty, price, liq), max(liq, 0) / 2 * F["max_fill_pct_of_quote_liq"] / 100)
    return (cap, True) if cap < quoted else (quoted, False)

def buy_multiplier(cfg, size_usd, liq, slippage_pct):
    """Effective buy price multiplier: max(assumed slippage, constant-product price impact)."""
    m = 1 + slippage_pct / 100
    if cfg_fills(cfg)["fill_cap"] and liq and liq > 0:
        r = liq / 2
        m = max(m, (r + size_usd) / r)
    return m

# ---------------------------------------------------------------- second source (GeckoTerminal, rarely)
def gt_slot_free():
    lim = LIMITS["gt"]
    return not lim.blocked() and lim.next_ok <= time.time() + 20

def gt_pool(cc, pair, token):
    """GeckoTerminal price/liquidity for a pool: {'price','liq'} | {'missing': True} (404) | None (no answer / busy)."""
    if not gt_slot_free():
        return None
    j = http_get("gt", f"{GT}/networks/{cc['geckoterminal_id']}/pools/{pair}", retries=1)
    if j is None:
        return None
    if j.get("_http404"):
        return {"missing": True}
    a = ((j.get("data") or {}).get("attributes")) or {}
    rel = ((j.get("data") or {}).get("relationships")) or {}
    if not a:
        return None
    base = (((rel.get("base_token") or {}).get("data") or {}).get("id") or "").lower()
    price = fnum(a.get("base_token_price_usd")) if base.endswith((token or "").lower()) else fnum(a.get("quote_token_price_usd"))
    return {"price": price, "liq": fnum(a.get("reserve_in_usd")), "mcap": fnum(a.get("market_cap_usd")) or fnum(a.get("fdv_usd"))}

def agree(a, b, tol_pct):
    return good(a) and good(b) and abs(a / b - 1) * 100 <= tol_pct

def _same(cc, a, b):
    if cc.get("kind") == "evm":
        return (a or "").lower() == (b or "").lower()
    return a == b

# ---------------------------------------------------------------- entry guard
_SCAN = {}   # (chain, token) -> (ts, price, implied_supply): previous scan reading, in memory only

def note_scan(chain, token, price, mcap):
    if good(price):
        _SCAN[(chain, token)] = (now_ts(), price, (mcap / price) if good(mcap) else None)
    if len(_SCAN) > 50000:
        cut = now_ts() - 3600
        for k in [k for k, v in _SCAN.items() if v[0] < cut]:
            _SCAN.pop(k, None)

def check_entry(cfg, cc, chain, token, m, all_pairs, last_candle=None, candle_max_age_sec=1800):
    """(ok, reason). m = scanner.pair_metrics of the pool we would buy in; all_pairs = the token's DexScreener pairs
    (already fetched in the batched scan call); last_candle = [ts, o, h, l, c, v] from the candles the timing gate used."""
    G = cfg_guard(cfg)
    price, liq, mcap = m.get("price"), m.get("liq"), m.get("mcap")
    prev = _SCAN.get((chain, token))
    note_scan(chain, token, price, mcap)
    if not G["enabled"]:
        return True, "price guard off"
    if not good(price):
        return False, f"no usable price in the reading ({price!r})"
    if not good(liq):
        return False, f"no usable pool liquidity in the reading ({liq!r})"
    # 1) jump vs the previous scan reading of the same token (a persisting move is accepted on the next scan)
    if prev and now_ts() - prev[0] <= G["entry_jump_window_sec"] and good(prev[1]):
        ratio = price / prev[1]
        if ratio >= G["max_jump_up_factor"] or ratio <= 1 - G["max_drop_pct"] / 100:
            return False, f"price ${price:.4g} is {ratio:.2f}x the previous scan reading ${prev[1]:.4g} - waiting for confirmation"
        if prev[2] and good(mcap):
            dev = abs((mcap / price) / prev[2] - 1) * 100
            if dev > G["max_supply_deviation_pct"]:
                return False, f"market cap ${mcap:,.0f} inconsistent with price ${price:.4g} (implied supply off {dev:.0f}% vs last scan)"
    # 2) the token's other pools (same coin, decent liquidity) must roughly agree
    refs = []
    for p in all_pairs or []:
        if _same(cc, p.get("pairAddress"), m.get("pair")):
            continue
        pl, pp = fnum((p.get("liquidity") or {}).get("usd")), fnum(p.get("priceUsd"))
        if good(pl) and pl >= G["min_ref_pair_liq_usd"] and good(pp):
            refs.append(pp)
    if refs:
        refs.sort()
        med = refs[len(refs) // 2] if len(refs) % 2 else (refs[len(refs) // 2 - 1] + refs[len(refs) // 2]) / 2
        if abs(price / med - 1) * 100 > G["pair_mismatch_pct"]:
            return False, (f"price ${price:.4g} disagrees with the token's other pools (median ${med:.4g}, "
                           f"{(price / med - 1) * 100:+.0f}% > ±{G['pair_mismatch_pct']}%)")
    # 3) the GeckoTerminal candles the timing gate already used must roughly agree (second source, no extra call)
    if last_candle and len(last_candle) >= 5 and good(fnum(last_candle[4])):
        cts, close = float(last_candle[0]), float(last_candle[4])
        if now_ts() - cts <= candle_max_age_sec and abs(price / close - 1) * 100 > G["candle_mismatch_pct"]:
            return False, (f"price ${price:.4g} disagrees with GeckoTerminal's latest candle close ${close:.4g} "
                           f"({(price / close - 1) * 100:+.0f}% > ±{G['candle_mismatch_pct']}%)")
    return True, "price reading ok"

# ---------------------------------------------------------------- open positions
def check_reading(con, cfg, cc, pos, pair, source="dexscreener"):
    """Validate a reading for an open position. Returns (verdict, price, liq, note):
      'ok'     -> act normally on price/liq
      'rug'    -> pool drained / crashed (confirmed on two checks): sell everything now, fill capped by the pool
      'reject' -> do not act on this reading (no exit rule runs)"""
    G, RG = cfg_guard(cfg), cfg_rug(cfg)
    price = fnum(pair.get("priceUsd"))
    liq = fnum((pair.get("liquidity") or {}).get("usd"))
    pid, sym, chain, now = pos["id"], pos["symbol"], pos["chain"], now_ts()
    upd = lambda **kw: con.execute("UPDATE positions SET " + ",".join(f"{k}=?" for k in kw) + " WHERE id=?", (*kw.values(), pid))
    if not G["enabled"]:
        return ("ok", price, liq, "") if good(price) else ("reject", None, None, "no usable price")
    rej = lambda msg: (reject(con, chain, pos["token"], sym, msg), ("reject", None, None, msg))[1]
    last_seen = _get(pos, "guard_last_seen")
    upd(guard_last_seen=now)
    # 1) identity: same pool and same coin as when the position was opened
    if not _same(cc, pair.get("pairAddress"), pos["pair"]) or not _same(cc, (pair.get("baseToken") or {}).get("address"), pos["token"]):
        return rej(f"reading from a different pair/coin ({pair.get('pairAddress')}) ignored")
    if not good(price):
        return rej(f"no usable price in reading ({pair.get('priceUsd')!r})")
    if liq is not None and not math.isfinite(liq):
        liq = None
    # 2) market cap must imply the same supply as before
    mcap = fnum(pair.get("marketCap")) or fnum(pair.get("fdv"))
    if good(mcap):
        supply = mcap / price
        if _get(pos, "guard_supply"):
            dev = abs(supply / pos["guard_supply"] - 1) * 100
            if dev > G["max_supply_deviation_pct"]:
                return rej(f"market cap ${mcap:,.0f} inconsistent with price ${price:.4g} (implied supply off {dev:.0f}%)")
        else:
            upd(guard_supply=supply)
    last_good = _get(pos, "last_price") or pos["entry_price"]
    ratio = price / last_good
    entry_liq = _get(pos, "entry_liq", 0) or 0
    # 3) rug: pool drained, or liquidity pulled together with a price crash -> confirm on the next check, then sell
    why_rug = None
    if RG["enabled"] and liq is not None:
        if liq < RG["min_exit_liquidity_usd"]:
            why_rug = f"pool liquidity ${liq:,.0f} < ${RG['min_exit_liquidity_usd']:,.0f} (was ${entry_liq:,.0f} at entry)"
        elif entry_liq and liq < entry_liq * (1 - RG["liq_drain_pct"] / 100):
            why_rug = f"pool liquidity down {(1 - liq / entry_liq) * 100:.0f}% since entry (${entry_liq:,.0f} -> ${liq:,.0f})"
        elif (entry_liq and liq < entry_liq * (1 - RG["crash_liq_drop_pct"] / 100)
              and price <= pos["entry_price"] * (1 - RG["crash_price_drop_pct"] / 100)):
            why_rug = (f"price crashed {(1 - price / pos['entry_price']) * 100:.0f}% while liquidity fell "
                       f"{(1 - liq / entry_liq) * 100:.0f}% (${entry_liq:,.0f} -> ${liq:,.0f})")
    if why_rug:
        if ratio >= 1.5:
            reject(con, chain, pos["token"], sym, f"price ${price:.4g} ({ratio:.1f}x last good) on a drained pool - not a real price")
        dts = _get(pos, "guard_drain_ts")
        if dts and now - dts <= G["confirm_window_sec"]:
            upd(guard_drain_ts=None)
            return ("rug", min(price, last_good), liq, why_rug)
        upd(guard_drain_ts=now)
        return rej(f"{why_rug} - looks like a rug, waiting for confirmation on the next check")
    elif _get(pos, "guard_drain_ts"):
        upd(guard_drain_ts=None)
    # 4) extreme moves and the first reading after a long gap need confirmation
    extreme = ratio >= G["max_jump_up_factor"] or ratio <= 1 - G["max_drop_pct"] / 100
    stale = last_seen is not None and now - last_seen > G["stale_gap_min"] * 60
    if extreme or stale:
        why = (f"{ratio:.2f}x vs last good ${last_good:.4g}" if extreme else f"first reading after a {(now - last_seen) / 60:.0f}-min data gap")
        spike_up = extreme and ratio > 1
        gt = None
        if extreme and G["use_second_source"] and source == "dexscreener":
            con.commit()   # never hold the DB write lock across a network call
            gt = gt_pool(cc, pos["pair"], pos["token"])
        if gt is not None and not gt.get("missing"):
            gl = gt.get("liq")
            gt_drained = gl is not None and gl < RG["min_exit_liquidity_usd"]
            if agree(price, gt.get("price"), G["confirm_tolerance_pct"]) and not (spike_up and gt_drained):
                upd(guard_pending_price=None, guard_pending_ts=None)
                log.info("[PRICE-GUARD] %s %s: %s confirmed by GeckoTerminal ($%.4g)", chain, sym, why, gt["price"])
                return ("ok", price, liq, f"{why}, confirmed by GeckoTerminal")
            if spike_up:
                upd(guard_pending_price=None, guard_pending_ts=None)
                return rej(f"reading ${price:.4g} ({why}) contradicted by GeckoTerminal (${gt.get('price') or 0:.4g}, liquidity ${gl or 0:,.0f})")
        pend, pts = _get(pos, "guard_pending_price"), _get(pos, "guard_pending_ts", 0)
        if pend and now - pts <= G["confirm_window_sec"] and agree(price, pend, G["confirm_tolerance_pct"]):
            m5 = (pair.get("txns") or {}).get("m5") or {}
            traded = (fnum((pair.get("volume") or {}).get("m5"), 0) or 0) > 0 and (m5.get("buys", 0) + m5.get("sells", 0)) > 0
            if not spike_up or traded or source != "dexscreener":
                upd(guard_pending_price=None, guard_pending_ts=None)
                log.info("[PRICE-GUARD] %s %s: %s confirmed on next check, acting on it", chain, sym, why)
                return ("ok", price, liq, f"{why}, confirmed on next check")
            return rej(f"spike ${price:.4g} repeated but with no trades in the last 5 min - not acted on")
        upd(guard_pending_price=price, guard_pending_ts=now)
        return rej(f"reading ${price:.4g} ({why}) held for confirmation")
    if _get(pos, "guard_pending_price"):
        upd(guard_pending_price=None, guard_pending_ts=None)
    return ("ok", price, liq, "")

def pool_missing(con, cfg, cc, pos):
    """The pool was absent from an ANSWERED DexScreener batch. After pool_missing_checks misses in a row ask GeckoTerminal:
    404 -> ('rug', last price, 0 liquidity); a GT reading -> ('reading', pseudo-pair) to run through check_reading;
    otherwise ('wait', None)."""
    RG = cfg_rug(cfg)
    n = int(_get(pos, "guard_missing", 0)) + 1
    con.execute("UPDATE positions SET guard_missing=? WHERE id=?", (n, pos["id"]))
    if not RG["enabled"] or n < RG["pool_missing_checks"]:
        return ("wait", None)
    con.commit()   # never hold the DB write lock across a network call
    gt = gt_pool(cc, pos["pair"], pos["token"])
    if gt is None:
        return ("wait", None)
    if gt.get("missing"):
        return ("rug", {"price": _get(pos, "last_price") or pos["entry_price"], "liq": 0.0,
                        "note": f"pool removed (missing from DexScreener {n} checks in a row, GeckoTerminal: no such pool)"})
    pseudo = {"pairAddress": pos["pair"], "baseToken": {"address": pos["token"]}, "priceUsd": gt.get("price"),
              "liquidity": {"usd": gt.get("liq")}}   # no market cap: GT's figure is not comparable with DexScreener's
    return ("reading", pseudo)
