# `site/data.json` — data format (schema_version 1)

Written by the crosschain bot every **30 seconds** to `/workspace/crosschain/site/data.json`
(atomic replace, so a reader never sees a half-written file). The local dashboard serves the same
object live at `http://localhost:8797/api/state` and the file at `http://localhost:8797/data.json`.

**PAPER TRADING ONLY**: every amount is fake money. The file contains no keys, tokens or wallets.

Conventions
- Timestamps are **Unix seconds (UTC epoch, float)**. Show them in `America/Toronto`.
  A few `*_iso` fields are provided already formatted with the Toronto offset.
- Money is **US dollars** (float). Percentages are plain numbers (`5.2` = 5.2 %).
- `chain` is one of `"solana"`, `"bsc"`, `"robinhood"` (keys of `chains`).
- `bucket` is the token's age bucket: `"new"` (pool younger than 24 h) or `"established"`.
  There is **one strategy** (`strategy = "combined"`); buckets only change thresholds and exits.
- Any number may be `null` when unknown. New keys may be added without bumping
  `schema_version`; removed/renamed keys bump it.

## Top level

| key | type | meaning |
|---|---|---|
| `schema_version` | int | format version (1) |
| `paper_trading_only` | bool | always `true` |
| `generated`, `generated_iso` | ts, string | when this file was built |
| `health` | object | *(Oct 6)* liveness: `{last_cycle_ts, heartbeat_ts, offline_after_sec}` (below) |
| `timezone` | string | `"America/Toronto"` |
| `account` | object | the single fake account (below) |
| `breakdown` | object | P&L split `by_chain` and `by_bucket` (below) |
| `chains` | object | one entry per configured chain (below) |
| `scanner` | object | scanner process status |
| `funnel` | object | last scan cycle's filter funnel (below) |
| `open_positions` | array | open paper positions (Position) |
| `closed_trades` | array | last 300 closed paper trades, newest first (Position) |
| `feed` | array | every token evaluated in the last cycle (Evaluation), passes/deep stages first, max 400 |
| `deep` | array | evaluations that reached the safety stage or later in the last 6 h, newest first, max 150 |
| `decisions` | array | last 100 decisions (all kinds, both accounts), newest first; at most 25 of them are `SCORE` rows (Oct 6). Each has `score_old` / `score_new` (Oct 6) |
| `equity_curve` | array | `{ts, equity}` about once a minute, last 14 days (thinned to ≤ 1,500 points) |
| `cycles` | array | last 20 scan cycles `{id, started, finished, discovered, new_tokens, evaluated, passed, api_calls}` |
| `telegram` | object | `{status, outbox}` — status is `waiting_for_token`, `waiting_for_start`, `connected` or `disabled`; outbox counts by state (`pending`, `sent`, `expired`, `failed`) |
| `strategy` | object | `{name: "combined", label, buckets: {new, established}}` human descriptions |
| `config` | object | the full current `config.toml` as JSON (read-only; no secrets live there) |
| `accounts` | object | *(Oct 3, side-by-side test)* `{current: AccountView, new: AccountView}` (below) |
| `comparison` | object | *(Oct 3)* comparison strip since the test started (below) |
| `scoring_test` | object | *(Oct 3)* `{model_version, threshold_new, threshold_current:{new, established}, selftest:{ok, msg, version, ts, ...}, enabled, tabs:{current:"#current", new:"#new"}}` |

**Side-by-side scoring test (Oct 3, 2026).** There are two fake accounts: `current` (the original one,
rule score) and `new` (scoring_v1 model score). All **older top-level fields keep their meaning and describe
the `current` account only**: `account`, `breakdown`, `open_positions`, `closed_trades`, `equity_curve`.
The new account is only in `accounts.new`. `feed`, `deep`, `decisions`, `chains`, `funnel` and `cycles` are shared
(one scan for both accounts); decisions carry a `scoring` field.

## `health` (Oct 6, 2026)

`{"last_cycle_ts": 1791230000.1, "heartbeat_ts": 1791230042.7, "offline_after_sec": 600}`

| key | type | meaning |
|---|---|---|
| `last_cycle_ts` | ts or `null` | when the last **completed** scan cycle finished (`cycles.finished` of the newest finished cycle) |
| `heartbeat_ts` | ts or `null` | last heartbeat of the live trading process (`bot.py`). A small heartbeat thread inside the bot writes the `state` key `bot_heartbeat` about **every 60 s** (also once at bot start). The web/dashboard process never writes it, so it goes stale when the bot dies or hangs at the process level. `null` = never written |
| `offline_after_sec` | int | `600`. Show the bot as **offline** when `generated - heartbeat_ts > offline_after_sec` (or `heartbeat_ts` is `null`). A `last_cycle_ts` much older than `scanner.interval_sec` (90 s) while the heartbeat is fresh means the bot is alive but scans are stuck/slow |

Note `data.json` itself is written by the bot's export thread, so a stale `generated` also means the bot is down;
the local dashboard's `/api/state` is built by the web process and keeps updating `generated`, which is why `heartbeat_ts` exists.

## `account`

| key | meaning |
|---|---|
| `equity` | cash + what open positions would sell for now (after slippage, fees, tax, gas) |
| `cash` | fake cash not in positions |
| `start` | starting balance (1000) |
| `pnl`, `pnl_pct` | total P&L vs start |
| `closed`, `wins`, `win_rate` | closed trade count, winners, win % (`null` if none closed) |
| `open`, `max_open` | open positions / allowed total |
| `trades_today` | positions opened since midnight Toronto |
| `day_start_equity`, `day_pnl`, `day_cap` | daily loss cap bookkeeping (cap in $) |
| `paused_daily_cap` | `true` when the daily loss cap stopped new entries for today |
| `manual_pause` | `true` when paused via Telegram `/pause` |

## `accounts[scoring]` (AccountView, Oct 3)

`scoring` is `"current"` or `"new"`.

| key | meaning |
|---|---|
| `scoring`, `label` | `"current"` / `"new"`, and `"Current scoring"` / `"New scoring"` |
| `active` | `false` until the new account has been started (or if its model self-test failed before it ever started) |
| `account` | same keys as the top-level `account`, for this account, plus `losses`, `realized_pnl`, `expectancy` (mean P&L per closed trade, $), `avg_win`, `avg_loss` ($), `started` (ts the new account started; `null` for current) |
| `breakdown` | same shape as the top-level `breakdown`, this account only |
| `open_positions`, `closed_trades` | Position lists of this account (closed: last 300, newest first) |
| `equity_curve` | `{ts, equity}` of this account, last 14 days |
| `decisions` | *(Oct 6)* Decision list of this account, newest first, max 100 (max 25 `SCORE` rows): rows with `scoring` = this account plus shared rows (`scoring` = `null`). For `new`, shared rows are only the non-trade kinds (`GUARD`, `SKIP`, `SCORE`) since the new account started; for `current`, all `null` rows (this includes BUY/SELL from before Oct 3, which all belonged to the current account) |

## `comparison` (Oct 3)

`{test_started, test_started_iso, accounts:{current|new: {label, trades, open, closed, wins, win_rate, pnl, max_drawdown_pct, max_drawdown_usd}}, only_current_coins, only_new_coins, both_coins}`
— counts positions opened since `test_started` (when the new account started); `pnl` = realized + unrealized P&L
of those positions; drawdown is peak-to-trough on the account's equity points since then; `only_*_coins` /
`both_coins` count distinct (chain, token) pairs bought.

## `breakdown.by_chain[chain]` and `breakdown.by_bucket[bucket]`

Only chains/buckets that have had at least one trade appear.
`{trades, open, closed, wins, losses, win_rate, realized_pnl, unrealized_pnl, total_pnl, invested_open}`
— `realized_pnl` is from closed trades, `unrealized_pnl` from open ones at the latest price.

## `chains[chain]`

| key | meaning |
|---|---|
| `label` | display name ("Solana", "BSC", "Robinhood Chain") |
| `enabled` | scanned or not (`note` explains when disabled) |
| `kind` | `"solana"` or `"evm"` |
| `chain_id` | EVM chain id (56, 4663) or `null` |
| `dexscreener_slug`, `geckoterminal_id` | data-source ids |
| `max_open` | per-chain cap on open positions |
| `costs` | `{slippage_pct, fee_pct, gas_usd}` assumed per buy and per sell |
| `rpc_ok`, `rpc_head`, `rpc_checked` | public RPC health check (block number / slot), every 10 min |
| `safety_sources` | which services provide the safety data |
| `watchlist` | tokens currently being re-checked each cycle |
| `last_cycle` | this chain in the last cycle: `{discovered, evaluated, passed, by_bucket:{new, established}, deep_by_bucket, safety_checked, rejected:{stage:n}, pending:{stage:n}, watchlist}` |
| `today` | since midnight Toronto, each token counted once at its latest result: `{tokens, by_bucket, passed, rejected:{stage:n}, pending:{stage:n}}` |
| `note` | text or `null` |

## `funnel`

`stages` — ordered stage ids; `stage_labels` — id → readable label; `last_cycle` —
`{id, started, finished, discovered, new_tokens, evaluated, passed, funnel, per_chain, api_calls, errors}`
where `funnel = {evaluated, passed, passed_current, passed_new, rejected:{stage:n}, pending:{stage:n}}` for all chains
(`passed_current` / `passed_new`, Oct 3: how many of the passes were for each account's score gate) and
`per_chain[chain]` is the same shape as `chains[chain].last_cycle`.

Stage ids, in order: `data` (listed on DexScreener), `age`, `liquidity`, `volume`, `market_cap`,
`txns` (real buys & sells), `safety` (contract / honeypot / tax), `holders` (count &
concentration), `holder_trend`, `score`, `timing` (good-entry timing), `price_guard` (the price
reading looked wrong, so no buy on it; added Oct 3), `lp_lock` (pool money not locked, or lock status unknown,
so no buy; added Oct 3), `error`.
"Rejected" = failed that step this cycle (usually re-checked later); "pending" = waiting for data,
age or API budget.

## Position (items of `open_positions`, `closed_trades`)

| key | meaning |
|---|---|
| `id` | position id |
| `chain`, `bucket`, `strategy` | chain, age bucket at entry, always `"combined"` |
| `token`, `pair`, `symbol`, `url` | token address, pool address, ticker, DexScreener link |
| `opened_at`, `closed_at` | ts (`closed_at` null while open) |
| `entry_price` | market price at entry; `entry_eff` = all-in cost per token incl. slippage, fee, tax, gas |
| `qty`, `remaining_qty` | tokens bought / still held (after a partial take-profit) |
| `cost_usd` | dollars put in |
| `proceeds_usd` | dollars received from sells so far |
| `peak_price`, `last_price`, `last_update` | highest seen price, latest price, when |
| `status` | `"open"` or `"closed"` |
| `exit_reason` | plain-English reason (closed trades) |
| `tp1_done` | 1 after the first take-profit sold part of it |
| `score`, `entry_liq` | score and pool liquidity at entry |
| `entry_reason` | why the good-entry rule fired (dip %, run-up %, bounce %, volume ratio) |
| `buy_tax_pct`, `sell_tax_pct`, `gas_usd` | token tax (EVM) and fixed gas used in the simulation |
| `pnl_usd`, `pnl_pct` | P&L (realized for closed; current for open, incl. partial sells) |
| `targets` | *open only*: `{tp1, tp2, sl, trail, max_hold_until}` prices / ts (`trail` null until active) |
| `value_usd` | *open only*: proceeds so far + what the rest would sell for now |
| `entry_fill_price` | *(Oct 3)* modelled fill price per token at entry (worse of slippage and pool price impact), before fee/tax/gas |
| `last_liq` | *(Oct 3)* pool liquidity (USD) at the last accepted reading; used to cap sell fills |
| `guard_last_seen`, `guard_pending_price`, `guard_pending_ts`, `guard_drain_ts`, `guard_supply`, `guard_missing` | *(Oct 3)* price-guard working values: last reading time, an extreme reading waiting for confirmation, first sighting of a possible rug, implied token supply, pool-missing counter |
| `scoring` | *(Oct 3)* `"current"` or `"new"`: which paper account owns it (older rows: `"current"`) |
| `score_current`, `score_new`, `prob_new`, `model_version` | *(Oct 3)* both scores at entry: rule score, new score (0–100), new-score probability (0–1), `"scoring_v1 1.0"`. `score` keeps holding the current rule score |

`exit_reason` can now also be `rug (...)`. That means the pool was drained or removed, and everything was sold at once at a pool-limited price.
Sell fills are limited by the pool for every exit reason. For example, `proceeds_usd` is 0 when the pool vanished.

## Evaluation (items of `feed`, `deep`)

`{ts, chain, bucket, token, symbol, url, result, stage, reasons, score, metrics, score_new, prob_new, model_version, gate_current, gate_new}`
- `score`: current rule score (only computed at the score stage, as before).
- *(Oct 3)* `score_new` (0–100, = `prob_new` × 100, 1 decimal), `prob_new`, `model_version` (`"scoring_v1 1.0"`): the new
  score, computed from this row's `metrics` + `chain` + `bucket` for **every** evaluation that has metrics. `0.0` = out of
  the model's domain (market cap / liquidity < 1.3). `null` when there were no metrics.
- *(Oct 3)* `gate_current`, `gate_new`: `"pass"`, `"reject"` or `"n.a."` (score stage not reached / new scoring off).
  A `result = "pass"` row was a buy candidate for each account whose gate is `"pass"`.
- `result`: `"pass"`, `"reject"`, `"reject (final)"` (token dropped from the watchlist) or `"pending"`.
- `stage`: where it stopped (see stage ids).
- `reasons`: plain-English, `; `-separated (includes score parts and timing numbers).
- `metrics`: `{price, liq, vol_h1, vol_h24, mcap, age_min, buys_h1, sells_h1, chg_m5, chg_h1, chg_h24, timing?}`
  where `timing` (when candles were checked) = `{pullback_pct, rise_before_pct, bounce_pct, ma_rising_pct?, vol_recover_ratio}`.

## Decision (items of `decisions`)

`{id, ts, kind, chain, token, symbol, message, scoring, score_old, score_new}` — `scoring` *(Oct 3)* is `"current"`, `"new"` or `null`
(shared, e.g. price-guard and LP-lock rejections); messages of the new account start with `[NEW SCORING]`. `kind` ∈ `BUY`, `SELL` (full or partial; see
message), `SKIP` (passed but not bought: caps, cooldown, pause, LP-lock check), `PAUSE` (daily loss cap),
`GUARD` (Oct 3: a price reading was rejected, text starts with `[PRICE-GUARD]`).
BUY/SELL messages now include the modelled fill price.

*(Oct 6)* `score_old`, `score_new` (number or `null`), on every decision in the top-level `decisions` **and** in each
`accounts[scoring].decisions`:
- `score_old` = the **current** rule score (old scoring; `positions.score_current`, or the evaluation `score`).
- `score_new` = the **new** scoring_v1 score, 0–100 (`positions.score_new` / evaluation `score_new`).
- Written when the decision is made: BUY / SKIP / GUARD (entry) use the candidate's scores from this scan; SELL uses the
  position's entry scores; `SCORE` rows carry the scores just computed. Rows that were not given scores (and all rows
  from before Oct 6, back-filled once at bot start) get them by lookup: BUY/SELL/GUARD → the paper position of that
  coin (latest opened at or before the decision; same account when `scoring` is set), otherwise the evaluation of the
  same chain+token with a score that is nearest in time (within ±6 h). `null` = no score known (e.g. `PAUSE`, or a coin
  that never reached a scored evaluation; note the evaluation `score` (old) only exists from the score step on, while
  `score_new` exists for almost every evaluation).

*(Oct 6)* `kind = "SCORE"`: logged for every coin that **reaches the score step** (passed or turned down), at most once
per coin per 30 min, `scoring = null`. Message: `score step [bucket]: current score X (pass|reject), new score Y (pass|reject|n.a.) -> <result> at <stage>`.
Bookkeeping only: it does not change any gate. In `data.json`, decision lists are still capped at 100 rows and at most 25
of those are `SCORE` rows (the newest ones), so BUY/SELL/SKIP are not crowded out; all SCORE rows stay in the database.

`config` (top level) includes the new sections `guard`, `rug`, `fills` and `lpcheck` (Oct 3), like every other section of `config.toml`.

## Database changes for the side-by-side test (Oct 3)

- *(Oct 6)* `decisions`: new columns `score_old REAL`, `score_new REAL` (see Decision). New indexes `ix_eval_token`
  on `evaluations(token, ts)` (for the score lookup) and `ix_dec_kind` on `decisions(kind, id)`. `state` key
  `bot_heartbeat` = unix ts of the bot's last heartbeat (→ `health.heartbeat_ts`).
- `evaluations`: new columns `score_new REAL`, `prob_new REAL`, `model_version TEXT`, `gate_current TEXT`, `gate_new TEXT`.
  Evaluations are now kept **permanently** (the 36 h deletion was removed).
- `positions`: `scoring TEXT NOT NULL DEFAULT 'current'`, `score_current REAL`, `score_new REAL`, `prob_new REAL`, `model_version TEXT`.
- `equity`, `fills`, `position_ticks`: `scoring TEXT NOT NULL DEFAULT 'current'`. `decisions`: `scoring TEXT` (nullable).
- `state`: the current account keeps `cash`, `starting_balance`, `day`, `day_start_equity`, `paused_today`; the new
  account uses `new:cash`, `new:starting_balance`, `new:started`, `new:day`, `new:day_start_equity`, `new:paused_today`;
  `scoring_new_status` holds the last model self-test result. `manual_pause` (Telegram) is shared by both.

## Database-only tables (not in `data.json`; added Oct 3)

These tables live in `crosschain.db` and can be read directly with SQLite.

`position_ticks`: one row per open position on every price check.

| column | meaning |
|---|---|
| `id` | row id |
| `pos_id` | `positions.id` |
| `ts` | unix time |
| `chain` | chain |
| `price` | raw price reading (null when there was no data) |
| `liq` | raw pool liquidity USD reading |
| `pnl_pct` | position P&L % after this check (realized if it closed) |
| `guard` | `ok`, `reject` (reading ignored), `rug` (rug exit), `sell` (an exit rule sold), `no_data` (DexScreener cool-down / no answer), `missing` (pool absent from an answered batch) |
| `source` | `dexscreener` or `geckoterminal` |
| `note` | guard or exit message |

`fills`: one row per modelled paper buy or sell.

| column | meaning |
|---|---|
| `id` | row id |
| `pos_id` | `positions.id` |
| `ts` | unix time |
| `chain` | chain |
| `side` | `buy` or `sell` |
| `reason` | `entry` or the exit reason |
| `qty` | tokens |
| `quote_price` | market price used |
| `liq_usd` | pool liquidity used for the impact model |
| `fill_price` | modelled price per token after slippage/pool impact, before fee/tax/gas |
| `usd` | dollars paid (buy) or received after all costs (sell) |
| `impact_pct` | how much worse than the quote, % |
| `capped` | 1 if the pool limit (not plain slippage) set the price |
