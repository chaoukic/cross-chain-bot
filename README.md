# Crosschain — multi-chain crypto scanner with PAPER trading

> **PAPER TRADING ONLY — fake money.** This program has no wallet, no private keys and
> cannot buy or sell anything for real. It only *pretends* to trade so you can see how the
> strategy would have done. It is separate from memebot and copytrader and never touches them.

## What it does

1. **Watches three chains**: Solana, BNB Chain (BSC) and **Robinhood Chain** (Robinhood's
   Ethereum layer-2, mainnet since 1 July 2026, chain id 4663). Each chain can be switched on/off in
   `config.toml`.
2. **Finds tokens** from free public lists: GeckoTerminal "new pools", "trending pools" and
   "top-volume pools" for each chain (used sparingly, because memebot and copytrader share the same
   free GeckoTerminal allowance), plus DexScreener's latest token profiles / boosts / takeovers.
3. **One strategy for everything.** Brand-new launches and older coins go through the *same*
   funnel. Each token is put in an **age bucket** — `new` (pool younger than 24 h) or
   `established` (24 h up to ~6 months). Some thresholds and the exits differ per bucket.
   Every check writes its exact reason to the dashboard:
   - **Market data** (DexScreener): liquidity, 1h/24h volume, market cap, real buys *and* sells.
     New coins need ≥ $15k liquidity; established coins ≥ $100k liquidity, ≥ $1M market cap and ≥ 1,000 holders.
   - **Safety**
     - *Solana* (same as memebot): mint authority and freeze authority revoked (read from the
       blockchain), no Token-2022 traps (transfer fee/hook, changeable balances — from GoPlus),
       biggest wallets read from the chain with pool/LP/burn accounts excluded.
     - *BSC*: GoPlus token security + a honeypot.is buy/sell simulation — not a honeypot, buy/sell
       tax ≤ 10 %, no mint function, not an upgradeable proxy, no blacklist, no pause, no hidden
       owner, owner can't change the tax; new coins must have renounced ownership. Holder
       concentration excludes the pool/pair, the pool manager and burn/dead addresses.
     - *Robinhood Chain*: the same GoPlus checks (GoPlus supports it). honeypot.is does **not**
       support Robinhood Chain, so the tax is often "unknown" there — that costs 5 score points
       (or you can make it wait with `evm_unknown_tax = "pending"`).
   - **Holder trend**: price must not run up while the holder count stays flat.
   - **Score** 0–100 (liquidity, turnover, buy/sell balance, holder growth, concentration, age,
     momentum, minus tax). Parts are listed next to each token.
   - **Good-entry timing** (last gate, uses GeckoTerminal price candles):
     - *new coins* (5-minute candles): the price has dipped **8–30 %** from its 2-hour high after
       a run-up of at least +300 %, has bounced at least 1.5 % off the dip low, is not more than 3 %
       below its 2-hour average, is not spiking (≤ +8 % in 5 min) or dropping fast (≥ −6 % in 5
       min), the last 3 candles are not a red freefall, buys ≥ sells over 1 h, and volume is
       coming back (last 2 candles ≥ half the average).
     - *established coins* (hourly candles): the price has pulled back **7–25 %** from its 72-hour
       high while the 72-hour average is still **rising** (and the price is at most 2 % under it),
       there was a ≥ 15 % run-up before the high, it has bounced ≥ 1 % off the low, the last hour
       is not a spike (≤ +8 %) or a drop (≥ −5 %), no 4-red-candle freefall, buys/sells ≥ 0.9, and
       the last 3 hours' volume is at least 60 % of normal.
4. **Paper trades** with a fake $1,000 account shared by all chains (since Oct 3 there is a second one for
   the scoring test, see "Side-by-side scoring test" below): 2 % of equity per trade
   (never more than 2 % of the pool's liquidity), max 5 open positions (Solana 3, BSC 3, Robinhood
   2), per-chain slippage + fee + the token's own tax + a small fixed gas cost on BSC ($0.10) and
   Robinhood Chain ($0.05). If the day's loss reaches 5 % of the day-start balance, new entries
   pause until midnight Toronto time. The same token is not bought again within 24 h.
   - Exits for **new** coins: +40 % sells half, +100 % sells the rest, stop −20 %, trailing stop
     15 % (after +10 %), volume-dying exit, max hold 24 h.
   - Exits for **established** coins: +12 % sells half, +25 % sells the rest, stop −8 %,
     trailing stop 5 % (after +8 %), max hold 7 days.

## Safety layer added on Oct 3, 2026 (price guard, rug exit, realistic fills, LP lock)

These four changes are ported from memebot and follow the Oct 3 algorithm assessment. Their settings
are in the new `config.toml` sections `[guard]`, `[rug]`, `[fills]` and `[lpcheck]`. No existing setting changed.

- **Price guard** (`priceguard.py`). Every price reading is checked before the bot acts on it.
  - Readings that never count: zero or missing prices, "not a number" prices, and readings from a different pool or coin.
  - Readings that need a second confirmation: a jump of more than 5× from the last good price, a fall of more than 50%,
    a market cap that implies a different token supply, and the first reading after a data gap of more than 10 minutes.
    Confirmation comes from GeckoTerminal (rarely, only when it has a free slot) or from the next check.
  - Before a buy, the price must also roughly agree (within 30%) with the coin's other pools and with the latest
    GeckoTerminal candle that the timing check already loaded.
  - A rejected reading never causes a buy or a sell. It is logged as `[PRICE-GUARD]` in `logs/bot.log` and
    in the decisions list (kind `GUARD`). The guard makes **no extra DexScreener calls**.
- **Rug exit.** A rug is any of these: the pool's money falls 80% or more from the entry level, the pool
  falls below $1,000, liquidity halves while the price halves, or the pool disappears (missing from DexScreener 3 checks
  in a row and GeckoTerminal says it doesn't exist). The bot waits one check to confirm, then sells everything with
  exit reason `rug (...)`. The Telegram alert starts with **"🚨 RUG — <coin> sold for $X"**.
- **Realistic paper fills, for every buy and sell.** The fill price uses constant-product pool maths on the pool's
  current liquidity. A big sell into a thin pool gets a worse price. A sell never receives more than 50% of the
  pool's quote side (liquidity ÷ 2). If the pool is gone, the sale brings $0. Slippage, fees, tax and gas still apply on top.
  Open positions are valued the same way.
- **Pool-money (LP) lock check before every buy** (`lpcheck.py`).
  - *Solana*: RugCheck. For new coins, at least 90% of the LP in the pool being bought must be locked or burned.
    For established coins, at least one pool must meet that bar. RugCheck must not flag the coin as rugged or show any "danger" risk.
  - *BSC / Robinhood Chain*: the GoPlus `lp_holders` list from the safety lookup the bot already makes, so no extra call.
    Locked LP plus LP sent to burn/dead addresses must be at least 90%.
  - **Unknown = no buy (fail-closed)**, as in memebot and as the assessment recommends. If RugCheck doesn't answer, or
    GoPlus has no LP data (for example a V3/concentrated pool), the coin is not bought. It stays on the watchlist and
    is checked again later. `fail_closed = false` would allow unknown.
- **New records** in `crosschain.db`:
  - `position_ticks` gets one row per open position on every price check (about every 45 s): price, liquidity,
    P&L % and the guard result (`ok`, `reject`, `rug`, `sell`, `no_data`, `missing`). This makes exit rules testable later.
  - `fills` gets one row per paper buy or sell, with the quoted price, the modelled fill price, the dollars,
    the pool impact, and whether the pool cap applied.
  - `positions` has new columns: `entry_fill_price`, `last_liq` and the guard's working columns (`guard_*`).
- Tests: `.venv/bin/python -m unittest tests/test_guards.py tests/test_cycle_smoke.py tests/test_sidebyside.py tests/test_health_scores.py -v`. These run offline with a temporary database.

## Side-by-side scoring test (started Oct 3, 2026)

There are now **two fake $1,000 accounts** that watch the same coins and trade them by the same rules,
except for one step: the **score** that decides whether a coin is good enough.

- **Current scoring** is the original account. Nothing about it changed: same rule score (buy at ≥ 55),
  same balance and history.
- **New scoring** is a second, separate fake account (starts at $1,000). It uses a score from a small
  statistical model (`scoring_v1`, version 1.0) fitted by analysis-bot on this bot's own scan data. The new
  score is the model's estimated chance (in %) that the coin rises +20 % before it falls −25 % within an hour.
  It buys at a new score of **38.4 or more**, which is roughly the top 20 % of coins that reach the score step.
  The two scores are on different scales, so 38.4 can't be compared with 55.
  - In plain words, the model likes coins doing a lot of their trading right now (1-hour volume, 1-hour
    volume as a share of the day, volume against liquidity), with more buyers than sellers, younger pools,
    no 5-minute spike, and bigger average trades.
  - It was tested on one morning of data only (test AUC 0.60 vs 0.51 for the current score). Treat it as a trial.
    Expect weeks, not days, before the two tabs can be told apart from luck.
- **What is the same for both:** the coin list, every filter and safety check, the holder checks, the
  good-entry timing gate, the price guard, the LP-lock check, trade size (2 % of *that account's* equity, at
  most 2 % of pool liquidity), exits, rug exit and the fill model. Both accounts can hold the same coin at the same
  time. Each paper fill is worked out as if the other account didn't exist.
- **What is separate:** cash, equity, max open positions (5 total, per-chain caps), the 24 h re-entry
  cooldown, and the 5 % daily loss cap (one account can be paused for the day while the other keeps trading).
  Telegram `/pause` and `/resume` affect both.
- A coin rejected by one score can still be bought by the other account, but it must pass the timing gate
  first. If the GeckoTerminal price-candle budget runs out, the coin waits for the next cycle **for both** accounts.
- The model and scorer are **copied** into `scoring_v1/` (with checksums in `scoring_v1/SHA256SUMS.txt`), so a
  later re-fit in analysis-bot can't change the running test. On every start the bot re-scores 10 stored test rows
  and logs "NEW SCORING self-test PASSED" with the model version. If the check fails, the New scoring account
  is switched off (loud error in `logs/bot.log`; the dashboard and `/status` say so). The current account is not affected.
- Both scores are saved for **every** scanned coin (`evaluations.score` = current, `score_new`, `prob_new`,
  `model_version`, plus `gate_current` / `gate_new` = pass, reject or n.a.). Evaluations are now **kept
  permanently**. They used to be deleted after 36 hours; that's the only housekeeping change. The database grows
  by roughly 30,000 rows a day.
- Every position, fill, price check (`position_ticks`), equity point and buy/sell/skip/pause decision is tagged
  `scoring = 'current'` or `'new'`. Telegram alerts start with a line saying which account they are for
  ("🏷 Current scoring account" / "🏷 New scoring account") and buy alerts show both scores.
- **Dashboard:** two tabs, http://localhost:8797/#current and http://localhost:8797/#new. They have the same layout:
  balance, P&L, today, win rate, expectancy, average win/loss, equity curve, open positions and closed trades,
  each showing both scores. A comparison strip on top shows trades, win rate, P&L and max drawdown for each account
  since the test started, and how many coins only one account bought.
- Settings: `[scoring_new]` in `config.toml` (`enabled`, `min_score_to_buy`, `starting_balance_usd`).
- Tests: `.venv/bin/python -m unittest discover -s tests -v` (all offline). `.venv/bin/python newscore.py` runs the model self-test.

## Opening the dashboard

- **On this machine:** http://localhost:8797 (refreshes every 15 seconds; read-only). Tabs:
  http://localhost:8797/#current (Current scoring) and http://localhost:8797/#new (New scoring).
- Soft dark theme (colours are in `static/style.css`, same palette as memebot).
- It shows balance, P&L, a card per chain, P&L split by chain and by age bucket, open/closed
  trades with the reason each was bought, the scan funnel (filterable per chain) with rejection
  reasons, the live scan feed, the decision log and all current settings.
- Not published to the internet (no Netlify yet).

## Data for the admin website

`site/data.json` is rewritten every 30 seconds. Its format is documented in **`DATA_FORMAT.md`**.
`site/` also contains a static copy of the dashboard that reads `data.json` directly.

## Telegram alerts

Alerts for every paper buy, every sell (including partial take-profits) with P&L, daily-cap
pause/resume and process start/stop/restart are **queued in the database** (`outbox` table)
right now, but **nothing is sent** until a bot token exists. To switch it on:

1. Create a new bot with @BotFather (do *not* reuse memebot's or copytrader's bot).
2. Save the token as `CROSSCHAIN_TELEGRAM_TOKEN` (environment variable or the box secrets file).
3. Open the new bot in Telegram and press **Start** — it remembers your chat and only answers you.
   No restart needed; the Telegram process checks for the token every minute. Alerts older than
   6 hours are marked expired instead of being sent all at once.

Commands: `/status`, `/positions` (both show Current scoring and New scoring), `/pause`, `/resume` (both accounts), `/help`.

Alerts are written in plain English with bold labels (Telegram HTML; if Telegram ever rejects the
formatting, the same message is sent as plain text). "Bot started" alerts and "crashed and
restarted itself" alerts are sent at most once per 10 minutes. `python preview_alerts.py` prints
example alerts (add `--send` to send them to Telegram, each marked "EXAMPLE ONLY").
To restart just one part without a "crashed" alert: `./restart_part.sh bot` (or `web`, `telegram`).

## Start / stop / check

```bash
cd /workspace/crosschain
./run.sh        # start (or restart) everything in the background
./status.sh     # is it running? per-chain scan counts, last cycle
./stop.sh       # stop everything
```

Everything keeps running after you close the terminal. A crashed part restarts by itself within
10 seconds. Account, trades and history live in `crosschain.db`. (After a machine reboot, run
`./run.sh` again.) To start the fake account over: `./stop.sh`, delete `crosschain.db`, `./run.sh`.

**Self-healing venv (Oct 6, 2026).** After the box was moved/restarted, `.venv` vanished and the supervisors
crash-looped (`.venv/bin/python: No such file or directory`). Now `run.sh`, and `supervise.sh` before every
(re)start of a part, call `ensure_venv` from `venv_check.sh`: if `.venv/bin/python` is missing or cannot import
`requests`, `fastapi`, `uvicorn`, it runs `python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt`.
A `flock` on `.venv.lock` makes sure only one of the 3 supervisors rebuilds; the others wait and reuse the result.
If the rebuild fails (e.g. no network) the part is not started and is retried after 30 s, 60 s, ... up to 10 min,
with no Telegram alerts. Check by hand: `./venv_check.sh`; test against a throw-away path without touching the live
venv: `VENV=/tmp/cc_venv_test ./venv_check.sh`.

**Liveness for the admin site (Oct 6).** The bot writes a heartbeat (`state.bot_heartbeat`) about every 60 s;
`data.json` → `health` = `{last_cycle_ts, heartbeat_ts, offline_after_sec: 600}`. Decisions now carry
`score_old` (current rule score) and `score_new` (new model score), and a `SCORE` decision is logged for every
coin that reaches the score step (max once per coin per 30 min; max 25 of them in `data.json`). See `DATA_FORMAT.md`.

## Settings

All thresholds are in **`config.toml`** with plain-English comments. Save the file and the bot
uses the new values on its next cycle — no restart needed. (Changing the dashboard port needs
`CROSSCHAIN_PORT=xxxx ./run.sh`.)

## Files

| File | What it is |
|---|---|
| `config.toml` | all settings |
| `bot.py` | scan loop + position updater + `site/data.json` writer |
| `scanner.py` | discovery, filters, safety checks, score, good-entry timing |
| `trader.py` | the two paper accounts (fills, exits, daily cap, `fills` + `position_ticks` records) |
| `newscore.py`, `scoring_v1/` | New scoring: copied model + reference scorer, self-test (Oct 3 side-by-side test) |
| `priceguard.py` | price-reading guard, rug detection, pool-limited fill model (Oct 3) |
| `lpcheck.py` | LP-lock check before a buy: RugCheck (Solana), GoPlus `lp_holders` (BSC/Robinhood) (Oct 3) |
| `tests/` | offline unit tests (synthetic data) |
| `common.py` | database, rate limits, API helpers |
| `web.py`, `dashboard_data.py`, `static/` | local dashboard |
| `notify.py`, `telegram_bot.py` | Telegram alert queue and (disabled until a token exists) sender |
| `supervise.sh`, `run.sh`, `status.sh`, `stop.sh`, `restart_part.sh` | keep-alive and control scripts |
| `preview_alerts.py` | prints (or `--send`s) example Telegram alerts |
| `site/data.json`, `DATA_FORMAT.md` | data file for the admin website and its documentation |
| `logs/` | logs for bot, web and telegram |

## API budgets (shared machine)

GeckoTerminal is the scarce one: memebot uses up to 8 calls/min and copytrader 6/min from the same
IP, so this bot is capped at **4/min** and at most 2 discovery + 2 price-candle calls per 90 s
cycle, with results cached (candles 6 min for new coins, 45 min for established ones) and a
2-minute back-off after any "rate limited" answer. DexScreener is capped at 60/min, GoPlus and
honeypot.is at 20/min each, Solana public RPC at one call every 2 s. RugCheck (Solana LP lock, only for coins that
passed every other check, cached 10 min) is spaced at 10/min. The price guard and rug exit add no DexScreener calls.

## Honest limitations

- Very new EVM tokens (especially on Robinhood Chain) often aren't in GoPlus's holder index
  yet; they wait ("pending") until it appears. The Robinhood Chain block explorer's API is behind
  a Cloudflare check, so it can't be used as a backup.
- GoPlus only lists the top 10 holders on EVM chains, so the "top 10" figure is approximate.
- Tax on Robinhood Chain is usually unknown (no free simulator supports it yet).
- Paper fills use the DexScreener price plus assumed costs. Real fills can be worse, and prices
  are re-checked every ~45 s, so fast crashes can go past the stop loss.
- None of this proves the strategy makes money. It's a way to watch and learn.
