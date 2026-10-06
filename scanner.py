"""One combined pipeline for every chain: cheap checks first, safety next, good-entry timing last.
Data (all free, keyless, read-only): DexScreener, GeckoTerminal, GoPlus, honeypot.is, public RPCs.
PAPER TRADING ONLY."""
import json, math, time
from datetime import datetime
from common import (http_get, rpc, fnum, now_ts, log, CALLS, LIMITS, get_state, set_state, enabled_chains, ds_blocked)
import lpcheck, newscore

GT = "https://api.geckoterminal.com/api/v2"
DS = "https://api.dexscreener.com"
GOPLUS = "https://api.gopluslabs.io/api/v1"
HONEYPOT = "https://api.honeypot.is/v2/IsHoneypot"

# Quote / wrapped / stable coins are never traded (they are the other side of the pool)
SKIP_SYMBOLS = {"SOL", "WSOL", "USDC", "USDT", "USDC.E", "USDT.E", "BNB", "WBNB", "ETH", "WETH", "BTCB", "WBTC", "CBBTC",
                "BUSD", "FDUSD", "DAI", "USD1", "USDG", "USDE", "USDS", "PYUSD", "TUSD", "USDD", "LISUSD", "SLISBNB",
                "STETH", "WSTETH", "JITOSOL", "MSOL", "BNSOL", "JUPSOL", "USD₮0", "USDT0"}
SKIP_ADDR = {"So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
             "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
             "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c", "0x55d398326f99059ff775485246999027b3197955",
             "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", "0xe9e7cea3dedca5984780bafc599bd69add087d56",
             "0x2170ed0880ac9a755fd29b2688956bd959f933f8", "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c",
             "0xc5f0f7b66764f6ec8c8dff7ba683102295e16409"}
# Solana token-account owners that belong to AMMs / launchpads (pool reserves, not holders) - from memebot
KNOWN_POOL_OWNERS = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4 authority
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM authority
    "WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh",   # Raydium LaunchLab authority
    "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC",  # Meteora DAMM v2 pool authority
    "FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM",  # Meteora DBC pool authority
}
SOL_BURN = {"1nc1nerator11111111111111111111111111111111"}
EVM_DEAD = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead",
            "0xdead000000000000000042069420694206942069", "0x0000000000000000000000000000000000000001"}
STAGES = ["data", "age", "liquidity", "volume", "market_cap", "txns", "safety", "holders", "holder_trend",
          "score", "timing"]
DEEP_STAGES = ("safety", "holders", "holder_trend", "score", "timing")

def norm(chain_cfg, addr):
    return addr.lower() if chain_cfg.get("kind") == "evm" and addr else addr

def skip_token(addr, symbol):
    return (addr or "").lower() in SKIP_ADDR or addr in SKIP_ADDR or (symbol or "").upper() in SKIP_SYMBOLS

# ------------------------------------------------------------------ discovery
def _parse_pools(j, chain, cc, source):
    out = []
    for p in (j or {}).get("data", []) or []:
        a = p.get("attributes", {})
        rel = p.get("relationships", {})
        names = (a.get("name") or "?").split(" / ")
        pick = None
        for i, side in enumerate(("base_token", "quote_token")):
            tid = (rel.get(side, {}).get("data") or {}).get("id", "")
            addr = tid.split("_", 1)[1] if "_" in tid else tid
            sym = names[i].strip() if i < len(names) else "?"
            if addr and not skip_token(addr, sym):
                pick = (addr, sym)
                break
        if not pick:
            continue
        ts = None
        if a.get("pool_created_at"):
            try:
                ts = datetime.fromisoformat(a["pool_created_at"].replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = None
        out.append({"chain": chain, "token": norm(cc, pick[0]), "pool": a.get("address"), "symbol": pick[1][:24],
                    "dex": (rel.get("dex", {}).get("data") or {}).get("id"), "created": ts, "source": source})
    return out

GT_SOURCES = [("new_pools", "new_pools", {"page": 1}, "new_pools_every_min"),
              ("trending", "trending_pools", {"page": 1, "duration": "6h"}, "trending_every_min"),
              ("top_volume", "pools", {"page": 1, "sort": "h24_volume_usd_desc"}, "top_volume_every_min")]

def discover(con, cfg):
    sc = cfg["scanner"]
    chains = enabled_chains(cfg)
    last = get_state(con, "discovery_last", {}) or {}
    due = []
    t = now_ts()
    for chain, cc in chains.items():
        for name, path, params, key in GT_SOURCES:
            k = f"{chain}:{name}"
            every = sc.get(key, 10) * 60
            overdue = (t - last.get(k, 0)) / every
            if overdue >= 1:
                due.append((overdue + (0.5 if name == "new_pools" else 0), k, chain, cc, name, path, params))
    due.sort(key=lambda x: -x[0])
    found, gt_used = [], 0
    for _, k, chain, cc, name, path, params in due[: int(sc.get("gt_discovery_calls_per_cycle", 2))]:
        j = http_get("gt", f"{GT}/networks/{cc['geckoterminal_id']}/{path}", params)
        if j is not None and j.get("_http404"):
            last[k] = now_ts(); continue
        if j is None:
            break  # GeckoTerminal busy / rate limited: try again next cycle
        gt_used += 1
        last[k] = now_ts()
        found += _parse_pools(j, chain, cc, "gt_" + name)
    set_state(con, "discovery_last", last)
    if sc.get("use_dexscreener_lists", True) and not ds_blocked() and now_ts() - last.get("ds_lists", 0) >= sc.get("dexscreener_lists_every_min", 5) * 60:
        last["ds_lists"] = now_ts()
        set_state(con, "discovery_last", last)
        con.commit()   # release the write lock before the DexScreener calls below (429 back-offs can sleep 30 s+)
        slug_to_chain = {cc["dexscreener_slug"]: (c, cc) for c, cc in chains.items()}
        for ep, src in (("token-profiles/latest/v1", "ds_profiles"), ("token-boosts/latest/v1", "ds_boosts"),
                        ("token-boosts/top/v1", "ds_top_boosts"), ("community-takeovers/latest/v1", "ds_takeovers")):
            if ds_blocked():
                break   # a 429 cool-down started: skip the remaining lists this time
            j = http_get("ds", f"{DS}/{ep}")
            for x in j if isinstance(j, list) else []:
                hit = slug_to_chain.get(x.get("chainId"))
                if hit and x.get("tokenAddress") and not skip_token(x["tokenAddress"], ""):
                    found.append({"chain": hit[0], "token": norm(hit[1], x["tokenAddress"]), "pool": None,
                                  "symbol": "?", "dex": None, "created": None, "source": src})
    return found, gt_used

def register(con, found):
    new = 0
    t = now_ts()
    for f in found:
        tid = f"{f['chain']}:{f['token']}"
        cur = con.execute("INSERT OR IGNORE INTO tokens(id,chain,address,pool_address,symbol,dex,pool_created_ts,first_seen,last_listed,source,status) "
                          "VALUES(?,?,?,?,?,?,?,?,?,?, 'watch')",
                          (tid, f["chain"], f["token"], f["pool"], f["symbol"], f["dex"], f["created"], t, t, f["source"]))
        if cur.rowcount:
            new += 1
        else:
            con.execute("UPDATE tokens SET last_listed=?, status=CASE WHEN status='expired' THEN 'watch' ELSE status END WHERE id=?", (t, tid))
    return new

# ------------------------------------------------------------------ market data (DexScreener)
def ds_tokens(chain_cfg, addrs, unavailable=None):
    """DexScreener pairs (token is the base) for up to 30 addresses per call.
    If `unavailable` (a set) is given, addresses whose batch got no answer (429 cool-down, error) are added to it,
    so the caller can treat them as "no data this cycle" instead of "not listed"."""
    pairs = {}
    slug = chain_cfg["dexscreener_slug"]
    for i in range(0, len(addrs), 30):
        chunk = addrs[i:i + 30]
        j = None if ds_blocked() else http_get("ds", f"{DS}/tokens/v1/{slug}/{','.join(chunk)}")
        if not isinstance(j, list) and unavailable is not None:
            unavailable.update(chunk)
        if isinstance(j, list):
            for p in j:
                b = (p.get("baseToken") or {}).get("address")
                if b:
                    pairs.setdefault(norm(chain_cfg, b), []).append(p)
    return pairs

def ds_pairs(chain_cfg, pair_addrs, answered=None):
    """Batched pair lookup (30 per call). If `answered` (a set) is given, the addresses whose batch got a real answer are
    added to it, so a pool missing from an answered batch can be told apart from "no data this round"."""
    out = {}
    for i in range(0, len(pair_addrs), 30):
        if ds_blocked():
            break   # global DexScreener cool-down: prices unavailable this round (positions keep their last price)
        j = http_get("ds", f"{DS}/latest/dex/pairs/{chain_cfg['dexscreener_slug']}/{','.join(pair_addrs[i:i + 30])}")
        if isinstance(j, dict) and answered is not None:
            answered.update(pair_addrs[i:i + 30])
        for p in (j or {}).get("pairs") or []:
            out[p.get("pairAddress")] = p
            out[(p.get("pairAddress") or "").lower()] = p
    return out

def pair_metrics(p, all_pairs):
    liq = fnum((p.get("liquidity") or {}).get("usd"))
    vol = p.get("volume") or {}
    tx = p.get("txns") or {}
    pc = p.get("priceChange") or {}
    created = p.get("pairCreatedAt")
    return {
        "pair": p.get("pairAddress"), "url": p.get("url"), "dex": p.get("dexId"),
        "symbol": (p.get("baseToken") or {}).get("symbol"), "name": (p.get("baseToken") or {}).get("name"),
        "price": fnum(p.get("priceUsd")), "liq": liq,
        "vol_h1": fnum(vol.get("h1"), 0), "vol_h6": fnum(vol.get("h6"), 0), "vol_h24": fnum(vol.get("h24"), 0),
        "buys_m5": (tx.get("m5") or {}).get("buys", 0), "sells_m5": (tx.get("m5") or {}).get("sells", 0),
        "buys_h1": (tx.get("h1") or {}).get("buys", 0), "sells_h1": (tx.get("h1") or {}).get("sells", 0),
        "buys_h24": (tx.get("h24") or {}).get("buys", 0), "sells_h24": (tx.get("h24") or {}).get("sells", 0),
        "chg_m5": fnum(pc.get("m5"), 0), "chg_h1": fnum(pc.get("h1"), 0), "chg_h6": fnum(pc.get("h6"), 0),
        "chg_h24": fnum(pc.get("h24"), 0),
        "mcap": fnum(p.get("marketCap")) or fnum(p.get("fdv")),
        "age_min": (time.time() - created / 1000) / 60 if created else None,
        "pool_set": [x.get("pairAddress") for x in all_pairs if x.get("pairAddress")],
    }

def best_pair(pairs, known_pool):
    with_liq = [p for p in pairs if fnum((p.get("liquidity") or {}).get("usd"))]
    if with_liq:
        return max(with_liq, key=lambda p: fnum(p["liquidity"]["usd"]))
    for p in pairs:
        if p.get("pairAddress") == known_pool:
            return p
    return pairs[0] if pairs else None

def bucket_of(cfg, age_min):
    return "new" if age_min is not None and age_min < cfg["buckets"]["new"]["max_age_hours"] * 60 else "established"

# ------------------------------------------------------------------ safety: Solana (memebot's checks + GoPlus extras)
def fetch_holders_rpc(cc, token, pool_set, supply):
    """Top wallets from the chain, excluding pool/LP/burn accounts. {} if the RPC refused. (memebot logic)"""
    eps = cc.get("rpc_endpoints")
    la = rpc(eps, "getTokenLargestAccounts", [token])
    accts = (la or {}).get("value") or []
    if not accts:
        return {}
    own = rpc(eps, "getMultipleAccounts", [[x["address"] for x in accts], {"encoding": "jsonParsed"}])
    owners = []
    for v in ((own or {}).get("value") or []):
        try:
            owners.append(v["data"]["parsed"]["info"]["owner"])
        except Exception:
            owners.append(None)
    if len(owners) != len(accts):
        return {}
    pools = set(pool_set) | KNOWN_POOL_OWNERS
    holders = []
    for acc, ow in zip(accts, owners):
        amt = int(acc.get("amount", 0))
        if acc["address"] in pools or ow in pools or ow in SOL_BURN:
            continue
        holders.append((amt, ow))
    holders.sort(key=lambda h: -h[0])
    return {"top1_pct": holders[0][0] / supply * 100 if holders else 0.0,
            "top10_pct": sum(h[0] for h in holders[:10]) / supply * 100, "holders_src": "solana_rpc"}

def safety_solana(cfg, cc, token, pool_set):
    d = {"ts": now_ts(), "src": [], "kind": "solana"}
    m = rpc(cc.get("rpc_endpoints"), "getAccountInfo", [token, {"encoding": "jsonParsed"}])
    info = (((m or {}).get("value") or {}).get("data") or {}).get("parsed", {}).get("info") if m else None
    if info:
        d["src"].append("solana_rpc")
        d["mint_authority"] = info.get("mintAuthority")
        d["freeze_authority"] = info.get("freezeAuthority")
        d["supply_raw"] = int(info.get("supply", 0))
        d["mint_known"] = True
    g = http_get("goplus", f"{GOPLUS}/solana/token_security", {"contract_addresses": token})
    r = next(iter(((g or {}).get("result") or {}).values()), None) if isinstance((g or {}).get("result"), dict) else None
    if r:
        d["src"].append("goplus")
        d["holders"] = int(fnum(r.get("holder_count"), 0) or 0) or None
        if not d.get("mint_known"):
            d["mint_authority"] = "set" if (r.get("mintable") or {}).get("status") == "1" else None
            d["freeze_authority"] = "set" if (r.get("freezable") or {}).get("status") == "1" else None
            d["mint_known"] = (r.get("mintable") or {}).get("status") is not None
        d["transfer_fee"] = bool(r.get("transfer_fee"))
        d["transfer_hook"] = bool(r.get("transfer_hook"))
        d["balance_mutable"] = (r.get("balance_mutable_authority") or {}).get("status") == "1"
        d["non_transferable"] = r.get("non_transferable") == "1"
        d["gp_holders"] = [{"owner": h.get("account"), "acct": h.get("token_account"), "pct": fnum(h.get("percent"), 0) * 100,
                            "tag": h.get("tag") or ""} for h in (r.get("holders") or [])]
    if d.get("supply_raw"):
        d.update(fetch_holders_rpc(cc, token, pool_set, d["supply_raw"]))
    if d.get("top1_pct") is None and d.get("gp_holders"):
        d.update(_holders_from_goplus_sol(d["gp_holders"], pool_set))
    d.pop("gp_holders", None) if d.get("holders_src") == "solana_rpc" else None
    return d

def _holders_from_goplus_sol(gp, pool_set):
    pools = set(pool_set) | KNOWN_POOL_OWNERS | SOL_BURN
    hs = [h for h in gp if h["owner"] not in pools and h["acct"] not in pools and "pool" not in h["tag"].lower()]
    return {"top1_pct": hs[0]["pct"] if hs else 0.0, "top10_pct": sum(h["pct"] for h in hs[:10]),
            "holders_src": "goplus (Solana RPC refused)"} if gp else {}

# ------------------------------------------------------------------ safety: EVM (GoPlus + honeypot.is)
def safety_evm(cfg, cc, token, pool_set):
    d = {"ts": now_ts(), "src": [], "kind": "evm"}
    g = http_get("goplus", f"{GOPLUS}/token_security/{cc['chain_id']}", {"contract_addresses": token})
    res = (g or {}).get("result") or {}
    r = res.get(token.lower()) or (next(iter(res.values()), None) if isinstance(res, dict) and res else None)
    if r:
        d["src"].append("goplus")
        flag = lambda k: {"1": True, "0": False}.get(str(r.get(k)), None)
        for k in ("is_honeypot", "is_mintable", "is_proxy", "is_blacklisted", "hidden_owner", "can_take_back_ownership",
                  "transfer_pausable", "slippage_modifiable", "is_open_source", "cannot_sell_all", "cannot_buy",
                  "selfdestruct", "owner_change_balance", "personal_slippage_modifiable", "trading_cooldown", "external_call"):
            d[k] = flag(k)
        bt, st = fnum(r.get("buy_tax")), fnum(r.get("sell_tax"))
        d["gp_buy_tax"] = bt * 100 if bt is not None else None
        d["gp_sell_tax"] = st * 100 if st is not None else None
        owner = (r.get("owner_address") or "").lower()
        d["owner"] = owner or None
        d["owner_renounced"] = (not owner) or owner in EVM_DEAD
        d["holders"] = int(fnum(r.get("holder_count"), 0) or 0) or None
        d["lp"] = lpcheck.evm_lp_summary(r)   # LP lock/burn share from the same GoPlus answer (None = no LP data)
        excl = {a.lower() for a in pool_set if a} | EVM_DEAD
        for x in r.get("dex") or []:
            for k in ("pair", "pool_manager"):
                if x.get(k):
                    excl.add(x[k].lower())
        hs = []
        for h in r.get("holders") or []:
            a = (h.get("address") or "").lower()
            tag = (h.get("tag") or "").lower()
            if a in excl or any(w in tag for w in ("pair", "pool", "lp", "burn", "dead", "null")):
                continue
            hs.append(fnum(h.get("percent"), 0) * 100)
        if r.get("holders"):
            d["top1_pct"] = hs[0] if hs else 0.0
            d["top10_pct"] = sum(hs[:10])
            d["holders_src"] = "goplus top-10 list (pools, pool manager, burn excluded)"
    if cc.get("honeypot_is", True):
        h = http_get("hp", HONEYPOT, {"address": token, "chainID": cc["chain_id"]})
        if h and h.get("_http404"):
            d["hp_note"] = "honeypot.is: " + str((h.get("body") or {}).get("error") or "not found") + " (GoPlus tax used)"
        elif h and ("simulationResult" in h or "honeypotResult" in h):
            d["src"].append("honeypot.is")
            d["hp_sim_ok"] = bool(h.get("simulationSuccess"))
            d["hp_is_honeypot"] = (h.get("honeypotResult") or {}).get("isHoneypot")
            d["hp_reason"] = (h.get("honeypotResult") or {}).get("honeypotReason")
            sim = h.get("simulationResult") or {}
            d["hp_buy_tax"], d["hp_sell_tax"] = fnum(sim.get("buyTax")), fnum(sim.get("sellTax"))
            d["hp_risk"] = (h.get("summary") or {}).get("risk")
            if not d.get("holders"):
                d["holders"] = int(fnum((h.get("token") or {}).get("totalHolders"), 0) or 0) or None
    # best tax figure: honeypot.is simulation, else GoPlus
    if d.get("hp_sim_ok") and d.get("hp_buy_tax") is not None:
        d["buy_tax"], d["sell_tax"], d["tax_src"] = d["hp_buy_tax"], d["hp_sell_tax"], "honeypot.is simulation"
    elif d.get("gp_buy_tax") is not None and d.get("gp_sell_tax") is not None:
        d["buy_tax"], d["sell_tax"], d["tax_src"] = d["gp_buy_tax"], d["gp_sell_tax"], "GoPlus"
    else:
        d["buy_tax"] = d["sell_tax"] = None
        d["tax_src"] = "unknown"
    return d

def check_safety(cfg, d, bucket_cfg):
    """Returns (hard_reject_reasons, final, notes)."""
    S = cfg["safety"]
    R, notes, final = [], [], False
    if d["kind"] == "solana":
        if S.get("require_mint_revoked", True) and d.get("mint_authority"):
            R.append("mint authority NOT revoked" + (" (unknown)" if d["mint_authority"] == "unknown" else ""))
        if S.get("require_freeze_revoked", True) and d.get("freeze_authority"):
            R.append("freeze authority NOT revoked" + (" (unknown)" if d["freeze_authority"] == "unknown" else ""))
        if d.get("transfer_fee"):
            R.append("Token-2022 transfer fee set (GoPlus)")
        if d.get("transfer_hook"):
            R.append("Token-2022 transfer hook (GoPlus)")
        if d.get("balance_mutable"):
            R.append("an authority can change balances (GoPlus)"); final = True
        if d.get("non_transferable"):
            R.append("non-transferable token"); final = True
        return R, final, notes
    # EVM
    if S.get("reject_honeypot", True) and (d.get("is_honeypot") or d.get("hp_is_honeypot") or d.get("cannot_sell_all")):
        R.append("HONEYPOT: " + ", ".join(x for x in [
            "GoPlus is_honeypot" if d.get("is_honeypot") else "", "honeypot.is simulation: " + str(d.get("hp_reason") or "cannot sell") if d.get("hp_is_honeypot") else "",
            "cannot sell all" if d.get("cannot_sell_all") else ""] if x)); final = True
    bt, st = d.get("buy_tax"), d.get("sell_tax")
    if bt is not None and bt > S["max_buy_tax_pct"]:
        R.append(f"buy tax {bt:.1f}% > {S['max_buy_tax_pct']}% ({d.get('tax_src')})")
    if st is not None and st > S["max_sell_tax_pct"]:
        R.append(f"sell tax {st:.1f}% > {S['max_sell_tax_pct']}% ({d.get('tax_src')})")
    checks = [("reject_mintable", "is_mintable", "owner can mint more tokens"), ("reject_proxy", "is_proxy", "upgradeable proxy contract"),
              ("reject_blacklist", "is_blacklisted", "contract has a blacklist"), ("reject_hidden_owner", "hidden_owner", "hidden owner"),
              ("reject_take_back_ownership", "can_take_back_ownership", "ownership can be taken back"),
              ("reject_pausable", "transfer_pausable", "transfers can be paused"),
              ("reject_slippage_modifiable", "slippage_modifiable", "owner can change the tax"),
              ("reject_slippage_modifiable", "personal_slippage_modifiable", "owner can set per-wallet tax"),
              ("reject_honeypot", "owner_change_balance", "owner can change balances"),
              ("reject_honeypot", "selfdestruct", "contract can self-destruct")]
    for opt, k, msg in checks:
        if S.get(opt, True) and d.get(k):
            R.append(msg)
    if S.get("reject_not_open_source", True) and d.get("is_open_source") is False:
        R.append("contract source not verified")
    if bucket_cfg.get("require_owner_renounced") and d.get("owner_renounced") is False:
        R.append(f"ownership not renounced (owner {d.get('owner', '')[:10]}…)")
    if "goplus" not in d.get("src", []):
        return ["GoPlus security data unavailable, retry next cycle"], False, ["pending"]
    if bt is None:
        notes.append("buy/sell tax could not be measured (no simulation for this chain/token)")
    return R, final, notes

# ------------------------------------------------------------------ scoring (one system, age-aware)
def score(m, d, bucket, B, notes):
    """Transparent rule-based score, 0-100. Each part is listed in the reasons."""
    f = {}
    minliq = max(B["min_liquidity_usd"], 1)
    f["liquidity"] = min(20, 20 * math.log10(max(m["liq"] / minliq, 1)) / math.log10(8))
    if bucket == "new":
        f["volume/liquidity"] = min(15, (m["vol_h1"] / m["liq"] if m["liq"] else 0) * 15)
    else:
        f["volume/liquidity"] = min(15, (m["vol_h24"] / m["liq"] if m["liq"] else 0) * 5)
    bs = m["buys_h1"] / max(m["sells_h1"], 1)
    f["buy/sell balance"] = 15 if 1.1 <= bs <= 2.5 else (10 if 0.9 <= bs < 1.1 or 2.5 < bs <= 4 else 3)
    tg = m.get("_trend")
    if tg is not None:
        hg, pc = tg
        f["holder growth vs price"] = max(0, min(20, 10 + (hg - max(pc, 0) / 4) * 2))
    elif d.get("holders"):
        f["holder growth vs price"] = 8 if d["holders"] >= 2 * B["min_holders"] else 5
    else:
        f["holder growth vs price"] = 4
    t1 = d.get("top1_pct")
    f["concentration"] = (15 * max(0, 1 - t1 / max(B["max_top_holder_pct"], 0.1))) if t1 is not None else 4
    age_h = (m["age_min"] or 0) / 60
    if bucket == "new":
        f["age sweet spot"] = 10 if 0.5 <= age_h <= 6 else (6 if age_h <= 12 else 3)
        ch = m["chg_h1"]
        f["momentum"] = 5 if 0 < ch <= 60 else (2 if 60 < ch <= 150 else 0)
    else:
        days = age_h / 24
        f["age sweet spot"] = 10 if 2 <= days <= 60 else (6 if days <= 120 else 3)
        f["momentum"] = 5 if -20 <= m["chg_h24"] <= 30 else 2
    pen = 0
    if d.get("kind") == "evm":
        if d.get("buy_tax") is None:
            pen += 5; notes.append("tax unknown (-5)")
        else:
            tx = (d["buy_tax"] or 0) + (d["sell_tax"] or 0)
            if tx > 0:
                pen += tx / 2; notes.append(f"buy+sell tax {tx:.1f}% (-{tx/2:.1f})")
    if bucket == "new" and m["chg_h1"] > 150:
        pen += 10; notes.append(f"already pumped {m['chg_h1']:.0f}% in 1h (-10)")
    total = max(0, min(100, sum(f.values()) - pen))
    return round(total, 1), {k: round(v, 1) for k, v in f.items()}

# ------------------------------------------------------------------ good-entry timing (GeckoTerminal candles)
def fetch_candles(cc, pool, token, minutes, limit):
    tf, agg = ("minute", minutes) if minutes < 60 else ("hour", minutes // 60)
    j = http_get("gt", f"{GT}/networks/{cc['geckoterminal_id']}/pools/{pool}/ohlcv/{tf}",
                 {"aggregate": agg, "limit": limit, "currency": "usd", "token": token})
    if j is None:
        return None
    if j.get("_http404"):
        return []
    lst = (((j or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return sorted([[float(x) for x in c] for c in lst], key=lambda c: c[0])  # [ts, o, h, l, c, v] old -> new

def sma(vals):
    return sum(vals) / len(vals) if vals else None

def timing_check(m, candles, T, bucket):
    """Pure function: (ok, reasons, details). Candles old->new."""
    R, det = [], {}
    price = m["price"]
    lb = int(T["lookback_candles"])
    need = max(12, lb // 2)
    if len(candles) < need:
        return None, [f"only {len(candles)} price candles so far (< {need}); waiting for more history"], det
    win = candles[-lb:]
    hi_idx = max(range(len(win)), key=lambda i: win[i][2])
    hi = win[hi_idx][2]
    pull = (hi - price) / hi * 100 if hi else 0
    det["pullback_pct"] = round(pull, 1)
    if pull < T["pullback_min_pct"]:
        R.append(f"no dip yet: {pull:.1f}% below the recent high (need {T['pullback_min_pct']}-{T['pullback_max_pct']}%)")
    elif pull > T["pullback_max_pct"]:
        R.append(f"dropped {pull:.1f}% from the recent high (> {T['pullback_max_pct']}%): looks like a dump, not a dip")
    # there must have been a run-up into the high (uptrend)
    before = candles[:len(candles) - lb + hi_idx + 1] if len(candles) > lb else win[:hi_idx + 1]
    before = before[-lb:]
    low_before = min(c[3] for c in before) if before else hi
    rise = (hi / low_before - 1) * 100 if low_before else 0
    det["rise_before_pct"] = round(rise, 1)
    if rise < T["min_rise_before_pct"]:
        R.append(f"no real run-up before the high (+{rise:.0f}% < +{T['min_rise_before_pct']}%)")
    after = win[hi_idx + 1:]
    low_since = min([c[3] for c in after] + [price])
    bounce = (price / low_since - 1) * 100 if low_since else 0
    det["bounce_pct"] = round(bounce, 1)
    if bounce < T["bounce_min_pct"]:
        R.append(f"still at the dip low (bounce {bounce:.1f}% < {T['bounce_min_pct']}%): not stabilised")
    closes = [c[4] for c in candles]
    n = int(T["trend_ma_candles"])
    ma = sma(closes[-n:])
    det["ma"] = ma
    if ma and price < ma * (1 - T["trend_ma_tolerance_pct"] / 100):
        R.append(f"price {((price / ma) - 1) * 100:.1f}% below its {n}-candle average (trend not up)")
    if bucket == "established" and T.get("ma_rising_candles"):
        k = int(T["ma_rising_candles"])
        if len(closes) >= n + k:
            ma_old = sma(closes[-n - k:-k])
            det["ma_rising_pct"] = round((ma / ma_old - 1) * 100, 2) if ma_old else None
            if ma_old and ma <= ma_old:
                R.append(f"{n}h average is falling ({(ma / ma_old - 1) * 100:.1f}% vs {k}h ago)")
        else:
            R.append(f"not enough hourly history to confirm the average is rising ({len(closes)} candles)")
    rn = int(T["freefall_red_candles"])
    last = candles[-rn:]
    if len(last) == rn and all(c[4] < c[1] for c in last):
        drop = (1 - last[-1][4] / last[0][1]) * 100 if last[0][1] else 0
        if drop > T["freefall_drop_pct"]:
            R.append(f"freefall: last {rn} candles red, -{drop:.1f}%")
    k = 2 if bucket == "new" else 3
    vols = [c[5] for c in win]
    avgv = sma(vols) or 0
    recent = sma([c[5] for c in candles[-k:]]) or 0
    det["vol_recover_ratio"] = round(recent / avgv, 2) if avgv else None
    if avgv and recent < T["volume_recover_ratio"] * avgv:
        R.append(f"volume not recovering (last {k} candles {recent / avgv:.2f}x the average, need {T['volume_recover_ratio']}x)")
    return (not R), R, det

def timing_precheck(m, T, bucket):
    """Cheap DexScreener-only parts of the timing rule, done before spending a GeckoTerminal call."""
    R = []
    if bucket == "new":
        if m["chg_m5"] > T["max_m5_change_pct"]:
            R.append(f"vertical spike: +{m['chg_m5']:.1f}% in 5 min (> {T['max_m5_change_pct']}%)")
        if m["chg_m5"] < T["min_m5_change_pct"]:
            R.append(f"dropping fast: {m['chg_m5']:.1f}% in 5 min")
    else:
        if m["chg_h1"] > T["max_h1_change_pct"]:
            R.append(f"1h spike +{m['chg_h1']:.1f}% (> {T['max_h1_change_pct']}%)")
        if m["chg_h1"] < T["min_h1_change_pct"]:
            R.append(f"falling hard: {m['chg_h1']:.1f}% this hour")
    bs = m["buys_h1"] / max(m["sells_h1"], 1)
    if bs < T["min_buy_sell_ratio_h1"]:
        R.append(f"buying pressure weak: 1h buys/sells {bs:.2f} < {T['min_buy_sell_ratio_h1']}")
    return R

# ------------------------------------------------------------------ evaluation
def evaluate(con, cfg, cc, tok, m, budget):
    """Returns (result, stage, reasons, score, final, extra). result: pass / reject / pending."""
    bucket = bucket_of(cfg, m["age_min"])
    B = cfg["buckets"][bucket]
    R = []
    age = m["age_min"]
    extra = {"bucket": bucket}
    if age is None:
        return "pending", "age", ["no pool creation time yet"], None, False, extra
    if bucket == "new" and age < B["min_pool_age_min"]:
        return "pending", "age", [f"pool age {age:.0f}m < {B['min_pool_age_min']}m (waiting)"], None, False, extra
    if bucket == "established" and age > B["max_age_days"] * 1440:
        return "reject", "age", [f"pool age {age/1440:.0f} days > {B['max_age_days']} days"], None, True, extra
    liq = m["liq"]
    if not liq:
        return "reject", "liquidity", ["no liquidity reported (bonding curve / unlisted)"], None, age > 120 and bucket == "new", extra
    if liq < B["min_liquidity_usd"]:
        final = bucket == "new" and age > 60 and liq < B["min_liquidity_usd"] * 0.25
        return "reject", "liquidity", [f"[{bucket}] liquidity ${liq:,.0f} < ${B['min_liquidity_usd']:,.0f}" + (" (dead)" if final else "")], None, final, extra
    if m["vol_h1"] < B["min_volume_h1_usd"]:
        R.append(f"1h volume ${m['vol_h1']:,.0f} < ${B['min_volume_h1_usd']:,.0f}")
    if m["vol_h24"] < B["min_volume_h24_usd"]:
        R.append(f"24h volume ${m['vol_h24']:,.0f} < ${B['min_volume_h24_usd']:,.0f}")
    if R:
        return "reject", "volume", [f"[{bucket}] " + "; ".join(R)], None, False, extra
    mc = m["mcap"]
    if mc is None:
        return "reject", "market_cap", ["market cap unknown"], None, False, extra
    if mc < B["min_market_cap_usd"] or mc > B["max_market_cap_usd"]:
        return "reject", "market_cap", [f"[{bucket}] market cap ${mc:,.0f} outside ${B['min_market_cap_usd']:,.0f}-${B['max_market_cap_usd']:,.0f}"], None, False, extra
    b, s = m["buys_h1"], m["sells_h1"]
    if b < B["min_buys_h1"]:
        R.append(f"only {b} buys in 1h (< {B['min_buys_h1']})")
    if s < B["min_sells_h1"]:
        R.append(f"only {s} sells in 1h (< {B['min_sells_h1']}) - possible honeypot")
    elif b and s / b < B["min_sell_buy_ratio"]:
        R.append(f"sell/buy ratio {s/b:.2f} < {B['min_sell_buy_ratio']} - possible honeypot")
    if m["buys_h24"] + m["sells_h24"] < B["min_txns_h24"]:
        R.append(f"{m['buys_h24'] + m['sells_h24']} txns/24h < {B['min_txns_h24']}")
    if R:
        return "reject", "txns", R, None, False, extra

    # ---- safety (expensive: cached, limited per cycle)
    d = json.loads(tok["safety_json"]) if tok["safety_json"] else None
    ttl = cfg["scanner"]["safety_cache_min_new" if bucket == "new" else "safety_cache_min_est"] * 60
    lp_missing = bool(d) and d.get("kind") == "evm" and "goplus" in (d.get("src") or []) and "lp" not in d  # cached before LP data was kept
    if not d or now_ts() - d.get("ts", 0) > ttl or not d.get("src") or lp_missing:
        if budget["safety"] <= 0:
            return "pending", "safety", ["safety lookup queued (per-cycle API budget used)"], None, False, extra
        budget["safety"] -= 1
        d = safety_solana(cfg, cc, tok["address"], m["pool_set"]) if cc["kind"] == "solana" else safety_evm(cfg, cc, tok["address"], m["pool_set"])
        budget["safety_done"] += 1
        con.execute("UPDATE tokens SET safety_json=?, safety_ts=? WHERE id=?", (json.dumps(d), d["ts"], tok["id"]))
        if d.get("holders"):
            last = con.execute("SELECT holders FROM holder_snaps WHERE token=? ORDER BY ts DESC LIMIT 1", (tok["id"],)).fetchone()
            if not last or last["holders"] != d["holders"]:
                con.execute("INSERT INTO holder_snaps(token,ts,holders,price) VALUES(?,?,?,?)", (tok["id"], d["ts"], int(d["holders"]), m["price"]))
        con.commit()   # don't hold the write lock across the RPC / candle calls below
    extra["safety"] = d
    if not d.get("src"):
        return "pending", "safety", ["safety data unavailable (API error), retry next cycle"], None, False, extra
    if d["kind"] == "solana" and not d.get("mint_known"):
        return "pending", "safety", ["mint/freeze authority unknown (RPC + GoPlus failed), retry next cycle"], None, False, extra
    hard, final, snotes = check_safety(cfg, d, B)
    if snotes == ["pending"]:
        return "pending", "safety", hard, None, False, extra
    if hard:
        return "reject", "safety", hard, None, final, extra
    if d["kind"] == "evm" and d.get("buy_tax") is None and cfg["safety"].get("evm_unknown_tax") == "pending":
        return "pending", "safety", ["buy/sell tax unknown; waiting (evm_unknown_tax = pending)"], None, False, extra
    notes = list(snotes)

    # ---- holders
    if d.get("top1_pct") is None and d["kind"] == "solana" and d.get("supply_raw") and budget.get("rpc_left", 0) > 0:
        # the public RPC refused the top-holder lookup earlier: retry just that part (as memebot does)
        budget["rpc_left"] -= 1
        h = fetch_holders_rpc(cc, tok["address"], m["pool_set"], d["supply_raw"])
        if h:
            d.update(h)
            con.execute("UPDATE tokens SET safety_json=? WHERE id=?", (json.dumps(d), tok["id"]))
            con.commit()   # don't hold the write lock across the candle call below
    t1, t10 = d.get("top1_pct"), d.get("top10_pct")
    if t1 is None:
        return "pending", "holders", ["top-holder data not available yet (RPC/GoPlus), retrying next cycle"], None, False, extra
    if t1 > B["max_top_holder_pct"]:
        R.append(f"top wallet holds {t1:.1f}% > {B['max_top_holder_pct']}% (pools/burn excluded)")
    if t10 is not None and t10 > B["max_top10_pct"]:
        R.append(f"top 10 wallets hold {t10:.1f}% > {B['max_top10_pct']}% (pools/burn excluded)")
    if d.get("holders") and d["holders"] < B["min_holders"]:
        R.append(f"{d['holders']} holders < {B['min_holders']}")
    if not d.get("holders") and bucket == "established":
        R.append("holder count unknown (established coins need a known holder count)")
    if R:
        return "reject", "holders", [f"[{bucket}] " + x for x in R], None, False, extra

    # ---- holder trend vs price
    H = cfg["holder_trend"]
    m["_trend"] = None
    snaps = con.execute("SELECT ts,holders,price FROM holder_snaps WHERE token=? AND ts>? ORDER BY ts",
                        (tok["id"], now_ts() - (3600 if bucket == "new" else 86400))).fetchall()
    if len(snaps) >= 2 and snaps[-1]["ts"] - snaps[0]["ts"] >= H["min_interval_min"] * 60:
        o, n = snaps[0], snaps[-1]
        hg = (n["holders"] - o["holders"]) / max(o["holders"], 1) * 100
        pc = ((m["price"] - o["price"]) / o["price"] * 100) if o["price"] else 0
        m["_trend"] = (hg, pc)
        mins = (now_ts() - o["ts"]) / 60
        if hg < -H["max_holder_drop_pct"]:
            return "reject", "holder_trend", [f"holders fell {hg:.1f}% in {mins:.0f}m"], None, False, extra
        if pc > H["price_up_pct"] and hg < H["min_holder_growth_pct"]:
            return "reject", "holder_trend", [f"price +{pc:.0f}% but holders only {hg:+.1f}% in {mins:.0f}m (one-buyer shape)"], None, False, extra
        notes.append(f"holders {hg:+.1f}% vs price {pc:+.1f}% over {mins:.0f}m")
    else:
        notes.append("holder trend not measurable yet (need two different holder counts)" if d.get("holders") else "holder count unknown; trend skipped")

    sc, parts = score(m, d, bucket, B, notes)
    notes.append("score parts: " + ", ".join(f"{k} {v}" for k, v in parts.items()))
    # ---- score gate, side-by-side test (2026-10-03): the current rule score decides for the 'current' account,
    # the scoring_v1 model score (same metrics dict, same moment) for the 'new' account. A coin that passes either
    # gate continues to the timing gate (which applies to both accounts); only coins failing BOTH stop here.
    sn = m.get("_new")
    if sn is None:
        sn = newscore.score(newscore.eval_metrics(m), tok["chain"], bucket)
    sn_score = sn[0] if sn else None
    new_on = newscore.enabled(cfg) and sn_score is not None
    g_cur = sc >= B["min_score_to_buy"]
    g_new = new_on and sn_score >= newscore.threshold(cfg)
    extra["gate_current"] = "pass" if g_cur else "reject"
    extra["gate_new"] = ("pass" if g_new else "reject") if new_on else "n.a."
    nthr = newscore.threshold(cfg)
    notes.append(f"new score {sn_score if sn_score is not None else 'n/a'} ({extra['gate_new']} at >= {nthr}; {newscore.MODEL_VERSION})")
    if not g_cur and not g_new:
        return "reject", "score", [f"[{bucket}] score {sc} < {B['min_score_to_buy']}"] + notes, sc, False, extra
    if not g_cur:
        notes.insert(0, f"current score {sc} < {B['min_score_to_buy']} (current account: no); new score {sn_score} >= {nthr} (new account only)")
    elif not g_new and new_on:
        notes.insert(0, f"new score {sn_score} < {nthr} (current account only)")

    # ---- good-entry timing (last gate)
    T = cfg["timing"][bucket]
    pre = timing_precheck(m, T, bucket)
    if pre:
        return "reject", "timing", [f"[{bucket}] entry timing: " + "; ".join(pre), f"score {sc}"] + notes, sc, False, extra
    tf = f"{int(T['candle_minutes'])}m"
    cached = json.loads(tok["candles_json"]) if tok["candles_json"] and tok["candles_tf"] == tf else None
    cttl = cfg["scanner"]["candle_cache_min_new" if bucket == "new" else "candle_cache_min_est"] * 60
    if cached is None or now_ts() - (tok["candles_ts"] or 0) > cttl:
        if budget["timing"] <= 0 or LIMITS["gt"].blocked():
            return "pending", "timing", [f"score {sc}; price-candle check queued (GeckoTerminal budget)"] + notes, sc, False, extra
        budget["timing"] -= 1
        limit = int(T["lookback_candles"] + T["trend_ma_candles"] + T.get("ma_rising_candles", 0))
        c = fetch_candles(cc, m["pair"], tok["address"], int(T["candle_minutes"]), min(limit, 1000))
        if c == [] and tok["pool_address"] and tok["pool_address"].lower() != (m["pair"] or "").lower() and budget["timing"] > 0:
            budget["timing"] -= 1   # GeckoTerminal doesn't know DexScreener's pool id: try the pool GeckoTerminal listed
            c = fetch_candles(cc, tok["pool_address"], tok["address"], int(T["candle_minutes"]), min(limit, 1000))
        if c is None:
            return "pending", "timing", [f"score {sc}; GeckoTerminal candles unavailable (busy), retry"] + notes, sc, False, extra
        cached = c
        con.execute("UPDATE tokens SET candles_json=?, candles_ts=?, candles_tf=? WHERE id=?", (json.dumps(c), now_ts(), tf, tok["id"]))
    if cached == []:
        return "reject", "timing", [f"[{bucket}] score {sc} but GeckoTerminal has no price candles for this pool (re-tried later)"] + notes, sc, False, extra
    ok, tr, det = timing_check(m, cached, T, bucket)
    extra["timing"] = det
    if ok is None:
        return "pending", "timing", [f"score {sc}; " + tr[0]] + notes, sc, False, extra
    if not ok:
        return "reject", "timing", [f"[{bucket}] score {sc} OK but entry timing not good: " + "; ".join(tr)] + notes, sc, False, extra
    why = (f"good entry: {det['pullback_pct']}% dip from recent high after +{det['rise_before_pct']}% run-up, "
           f"bounced {det['bounce_pct']}%, volume {det.get('vol_recover_ratio')}x avg")
    extra["entry_reason"] = why
    extra["last_candle"] = cached[-1] if cached else None   # for the entry price guard (second source, no extra call)
    extra["candle_max_age_sec"] = max(1800, 2 * int(T["candle_minutes"]) * 60 + cttl)
    return "pass", "timing", [f"PASSED all filters [{bucket}], score {sc}; {why}"] + notes, sc, False, extra

# ------------------------------------------------------------------ chain heartbeat (1 cheap RPC call per chain, every 10 min)
def chain_heartbeat(con, cfg):
    st = get_state(con, "chain_status", {}) or {}
    for chain, cc in (cfg.get("chains") or {}).items():
        s = st.get(chain, {})
        if not cc.get("enabled"):
            st[chain] = {"enabled": False, "note": "disabled in config.toml"}
            continue
        if now_ts() - s.get("rpc_checked", 0) < 600:
            continue
        if cc["kind"] == "solana":
            r = rpc(cc.get("rpc_endpoints"), "getSlot", [])
            s.update({"rpc_ok": r is not None, "head": r})
        else:
            r = rpc(cc.get("rpc_endpoints"), "eth_blockNumber", [], kind="evmrpc")
            cid = rpc(cc.get("rpc_endpoints"), "eth_chainId", [], kind="evmrpc")
            s.update({"rpc_ok": r is not None, "head": int(r, 16) if r else None,
                      "chain_id_ok": (int(cid, 16) == cc.get("chain_id")) if cid else None})
        s["rpc_checked"] = now_ts()
        s["enabled"] = True
        st[chain] = s
    set_state(con, "chain_status", st)
