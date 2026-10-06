// Crosschain dashboard (read-only). Loads /api/state (local server) or falls back to data.json (static copy).
const TZ = "America/Toronto";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const usd = (v, d = 2) => v == null ? "—" : (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d});
const pct = (v, d = 1) => v == null ? "—" : (v > 0 ? "+" : "") + v.toFixed(d) + "%";
const cls = (v) => v > 0 ? "pos" : v < 0 ? "neg" : "";
const price = (v) => v == null ? "—" : "$" + (v >= 1 ? v.toFixed(4) : Number(v).toPrecision(4));
const big = (v) => v == null ? "—" : v >= 1e9 ? "$" + (v / 1e9).toFixed(2) + "B" : v >= 1e6 ? "$" + (v / 1e6).toFixed(2) + "M" : v >= 1e3 ? "$" + (v / 1e3).toFixed(1) + "k" : "$" + Math.round(v);
const tfmt = (ts, withDate) => ts ? new Date(ts * 1000).toLocaleString("en-CA", {timeZone: TZ, hour12: false,
  ...(withDate ? {month: "short", day: "numeric"} : {}), hour: "2-digit", minute: "2-digit", second: withDate ? undefined : "2-digit"}) + " ET" : "—";
const ago = (ts, now) => { if (!ts) return "—"; const s = Math.max(0, now - ts); return s < 60 ? Math.round(s) + "s" : s < 3600 ? Math.round(s / 60) + "m" : s < 172800 ? (s / 3600).toFixed(1) + "h" : (s / 86400).toFixed(1) + "d"; };
const ageMin = (m) => m == null ? "—" : m < 60 ? Math.round(m) + "m" : m < 2880 ? (m / 60).toFixed(1) + "h" : (m / 1440).toFixed(0) + "d";
const CHAIN = {solana: "Solana", bsc: "BSC", robinhood: "Robinhood"};
const chip = (c) => c ? `<span class="chip ${esc(c)}">${esc(CHAIN[c] || c)}</span>` : "";
const bchip = (b) => b ? `<span class="chip ${esc(b)}">${esc(b)}</span>` : "";
const tok = (sym, url, addr) => url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(sym || addr?.slice(0, 6))}</a>` : esc(sym || addr?.slice(0, 6));
const table = (el, head, rows, emptyMsg) => {
  $(el).innerHTML = rows.length ? `<thead><tr>${head.map(h => `<th class="${h.startsWith("#") ? "num" : ""}">${esc(h.replace(/^#/, ""))}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody>`
    : `<tbody><tr><td class="empty">${esc(emptyMsg)}</td></tr></tbody>`;
};
let chart, DATA;
// Side-by-side scoring test (Oct 3): two paper accounts, one tab each (#current / #new). Older data.json files without
// `accounts` fall back to the legacy single-account fields (= the current account).
const LABEL = {current: "Current scoring", new: "New scoring"};
const tabOf = () => (location.hash || "").replace("#", "") === "new" ? "new" : "current";
let TAB = tabOf();
const view = (d) => (d.accounts && d.accounts[TAB]) || {scoring: "current", label: LABEL.current, active: true, account: d.account,
  breakdown: d.breakdown, open_positions: d.open_positions, closed_trades: d.closed_trades, equity_curve: d.equity_curve};
const sc1 = (v) => v == null ? "—" : Number(v).toFixed(1);
const gate = (g) => g === "pass" || g === "reject" ? `<span class="gate ${g}">${g}</span>` : "";
const scores = (cur, nw) => `<span class="small">cur ${sc1(cur)}<br>new ${sc1(nw)}</span>`;

function compare(d) {
  const c = d.comparison || {}, A = c.accounts || {}, st = d.scoring_test || {};
  const box = (k) => { const a = A[k]; if (!a) return `<div class="cmp"><div class="t">${LABEL[k]}</div><div class="muted small">no data yet</div></div>`;
    return `<div class="cmp"><div class="t">${esc(a.label)}</div><div class="kv">
      <span>Trades (open)</span><span>${a.trades} (${a.open})</span>
      <span>Win rate</span><span>${a.win_rate == null ? "—" : a.win_rate.toFixed(0) + "%"} <span class="muted small">${a.wins}/${a.closed}</span></span>
      <span>P&amp;L</span><span class="${cls(a.pnl)}">${usd(a.pnl)}</span>
      <span>Max drawdown</span><span>${a.max_drawdown_pct == null ? "—" : a.max_drawdown_pct.toFixed(2) + "%"} <span class="muted small">${usd(a.max_drawdown_usd)}</span></span></div></div>`; };
  const st0 = st.selftest || {};
  $("compare").innerHTML = `<h2>Side-by-side test <span class="muted small">since ${c.test_started ? tfmt(c.test_started, true) : "—"} ·
    new score = ${esc(st.model_version || "scoring_v1")} probability × 100, buys at ≥ ${st.threshold_new ?? "—"} · current score buys at ≥ ${(st.threshold_current || {}).new ?? "—"}
    ${st0.ok === false ? ' · <span class="neg">model self-test FAILED: new account off</span>' : ""}</span></h2>
    <div class="cmpgrid">${box("current")}${box("new")}
    <div class="cmp"><div class="t">Coins bought</div><div class="kv">
      <span>Only Current scoring</span><span>${c.only_current_coins ?? "—"}</span>
      <span>Only New scoring</span><span>${c.only_new_coins ?? "—"}</span>
      <span>Both</span><span>${c.both_coins ?? "—"}</span></div>
      <div class="muted small" style="margin-top:4px">Same coins, same checks, same exits and fills; only the score gate differs.</div></div></div>`;
}

function tabs(d) {
  document.querySelectorAll("#tabs .tab").forEach(a => a.classList.toggle("active", a.dataset.tab === TAB));
  const v = view(d);
  $("tabnote").textContent = TAB === "new" ? (v.active ? "Fake $1,000 test account · buys when the new score passes" : "New account not started yet")
    : "The original fake account · unchanged rules";
  document.querySelectorAll(".acctname").forEach(e => e.textContent = "· " + (v.label || LABEL[TAB]));
  $("eqnote").textContent = `(fake $, ${v.label || LABEL[TAB]} account)`;
}

function kpis(d) {
  const a = view(d).account, st = d.scanner || {}, now = d.generated;
  let stPill = `<span class="pill">unknown</span>`;
  const age = st.last_run ? now - st.last_run : null;
  if (st.state === "scanning") stPill = `<span class="pill ok">scanning…</span>`;
  else if (st.state === "error") stPill = `<span class="pill bad">error</span>`;
  else if (age != null) stPill = age < 400 ? `<span class="pill ok">running</span>` : `<span class="pill bad">stale</span>`;
  const tg = d.telegram || {};
  const cards = [
    ["Fake balance (equity)", usd(a.equity), `cash ${usd(a.cash)} · start ${usd(a.start, 0)}`],
    ["Total P&L", `<span class="${cls(a.pnl)}">${usd(a.pnl)}</span>`, `<span class="${cls(a.pnl)}">${pct(a.pnl_pct, 2)}</span>`],
    ["Win rate", a.win_rate == null ? "—" : a.win_rate.toFixed(0) + "%", `${a.wins} wins / ${a.closed} closed`],
    ["Expectancy / trade", a.expectancy == null ? "—" : `<span class="${cls(a.expectancy)}">${usd(a.expectancy)}</span>`,
      `avg win ${usd(a.avg_win)} · avg loss ${usd(a.avg_loss)}`],
    ["Open positions", `${a.open} / ${a.max_open}`, `trades today: ${a.trades_today}`],
    ["Scanner", stPill, `last run ${st.last_run ? tfmt(st.last_run) + " (" + ago(st.last_run, now) + " ago)" : "—"}`],
    a.manual_pause ? ["New entries", `<span class="pill bad">PAUSED</span>`, "via Telegram /pause"]
      : ["Daily loss cap", a.paused_daily_cap ? `<span class="pill bad">PAUSED</span>` : `<span class="pill ok">OK</span>`,
         `today <span class="${cls(a.day_pnl)}">${usd(a.day_pnl)}</span> · cap -${usd(a.day_cap)}`],
    ["Telegram", tg.status === "connected" ? `<span class="pill ok">on</span>` : `<span class="pill wait">off</span>`,
      tg.status === "waiting_for_token" ? `waiting for a bot token · ${(tg.outbox || {}).pending || 0} alerts queued` : esc(tg.status || "—")],
  ];
  $("kpis").innerHTML = cards.map(([l, v, s]) => `<div class="kpi"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${s}</div></div>`).join("");
}

function chainCards(d) {
  const html = Object.entries(d.chains).map(([name, c]) => {
    const lc = c.last_cycle || {}, td = c.today || {}, b = (view(d).breakdown.by_chain || {})[name] || {};
    const status = !c.enabled ? `<span class="pill">disabled</span>` : c.rpc_ok === false ? `<span class="pill wait">RPC down</span>` : `<span class="pill ok">scanning</span>`;
    const bb = lc.by_bucket || {};
    return `<div class="card chaincard"><h3>${chip(name)} ${status}</h3><div class="kv">
      <span>Watchlist</span><span>${c.watchlist}</span>
      <span>Last cycle: checked</span><span>${lc.evaluated ?? "—"} <span class="muted small">(new ${bb.new ?? 0} · est. ${bb.established ?? 0})</span></span>
      <span>Last cycle: reached safety+</span><span>${lc.deep_by_bucket ? (lc.deep_by_bucket.new || 0) + (lc.deep_by_bucket.established || 0) : "—"} · safety lookups ${lc.safety_checked ?? 0}</span>
      <span>Today: distinct tokens</span><span>${td.tokens ?? 0} · passed ${td.passed ?? 0}</span>
      <span>Trades (open)</span><span>${b.trades || 0} (${b.open || 0}) · max ${c.max_open}</span>
      <span>P&L</span><span class="${cls(b.total_pnl)}">${usd(b.total_pnl || 0)}</span>
      <span>Costs per side</span><span class="small">${c.costs.slippage_pct}% slip · ${c.costs.fee_pct}% fee${c.costs.gas_usd ? " · $" + c.costs.gas_usd + " gas" : ""}</span>
      <span>Safety data</span><span class="small">${esc(c.safety_sources.join(" + "))}</span>
      <span>Chain RPC</span><span class="small">${c.rpc_ok == null ? "—" : c.rpc_ok ? "ok, block/slot " + (c.rpc_head || "").toLocaleString() : "not reachable"}</span>
    </div>${c.note ? `<div class="note">${esc(c.note)}</div>` : ""}</div>`;
  }).join("");
  $("chaincards").innerHTML = html;
}

function breakdown(d) {
  const row = (k, b, label) => `<tr><td>${label}</td><td class="num">${b.trades}</td><td class="num">${b.open}</td><td class="num">${b.closed}</td>
    <td class="num">${b.win_rate == null ? "—" : b.win_rate.toFixed(0) + "%"}</td><td class="num ${cls(b.realized_pnl)}">${usd(b.realized_pnl)}</td>
    <td class="num ${cls(b.unrealized_pnl)}">${usd(b.unrealized_pnl)}</td></tr>`;
  const head = (h) => [h, "#Trades", "#Open", "#Closed", "#Win rate", "#Realized", "#Unrealized"];
  const bd = view(d).breakdown;
  table("bychain", head("Chain"), Object.entries(bd.by_chain).map(([k, b]) => row(k, b, chip(k))), "No paper trades yet on any chain.");
  table("bybucket", head("Age bucket"), Object.entries(bd.by_bucket).map(([k, b]) => row(k, b, bchip(k))), "No paper trades yet (new vs established breakdown appears here).");
}

function equityChart(d) {
  const v = view(d);
  const pts = (v.equity_curve || []).map(e => ({x: e.ts * 1000, y: e.equity}));
  const labels = pts.map(p => new Date(p.x).toLocaleString("en-CA", {timeZone: TZ, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false}));
  const css = getComputedStyle(document.documentElement);
  const grid = css.getPropertyValue("--border").trim(), txt = css.getPropertyValue("--text-muted").trim(), line = css.getPropertyValue("--accent").trim();
  const ds = {labels, datasets: [{data: pts.map(p => p.y), borderColor: line, backgroundColor: "rgba(143,168,200,0.10)", fill: true, pointRadius: 0, borderWidth: 2, tension: 0.2},
    {data: pts.map(() => v.account.start), borderColor: grid, borderDash: [5, 5], pointRadius: 0, borderWidth: 1}]};
  if (typeof Chart === "undefined") return;
  if (chart) { chart.data = ds; chart.update("none"); return; }
  chart = new Chart($("eqchart"), {type: "line", data: ds, options: {responsive: true, maintainAspectRatio: false, animation: false,
    plugins: {legend: {display: false}, tooltip: {callbacks: {label: c => usd(c.parsed.y)}}},
    scales: {x: {ticks: {color: txt, maxTicksLimit: 8}, grid: {color: grid}}, y: {ticks: {color: txt, callback: v => "$" + Number(v).toFixed(2)}, grid: {color: grid}}}}});
}

function positions(d) {
  const now = d.generated;
  const v = view(d);
  table("open", ["Token", "Chain", "Bucket", "#Current", "#P&L", "#Entry", "#Peak", "#Size", "Age", "Exit targets", "#Scores", "Why bought"], v.open_positions.map(p =>
    `<tr><td>${tok(p.symbol, p.url, p.token)}</td><td>${chip(p.chain)}</td><td>${bchip(p.bucket)}</td>
     <td class="num">${price(p.last_price)}</td><td class="num ${cls(p.pnl_usd)}">${usd(p.pnl_usd)}<br><span class="small">${pct(p.pnl_pct)}</span></td>
     <td class="num">${price(p.entry_price)}</td>
     <td class="num">${price(p.peak_price)}<br><span class="small muted">${p.peak_price && p.entry_price ? pct((p.peak_price / p.entry_price - 1) * 100) : ""}</span></td>
     <td class="num">${usd(p.cost_usd)}${p.tp1_done ? `<br><span class="small muted">${(p.remaining_qty / p.qty * 100).toFixed(0)}% left</span>` : ""}</td>
     <td>${ago(p.opened_at, now)}</td>
     <td class="small">TP1 ${price(p.targets.tp1)}${p.tp1_done ? " ✓" : ""} · TP2 ${price(p.targets.tp2)} · SL ${price(p.targets.sl)}${p.targets.trail ? " · trail " + price(p.targets.trail) + " (active)" : " · trail not active"}<br><span class="muted">max hold until ${tfmt(p.targets.max_hold_until, true)}</span></td>
     <td class="num">${scores(p.score_current ?? p.score, p.score_new)}</td>
     <td class="reasons small">${esc(p.entry_reason || "")}</td></tr>`), "No open paper positions right now.");
  table("closed", ["Token", "Chain", "Bucket", "Opened", "Closed", "#Size", "#Entry", "#Exit", "#P&L", "Exit reason", "#Scores"], v.closed_trades.map(p =>
    `<tr><td>${tok(p.symbol, p.url, p.token)}</td><td>${chip(p.chain)}</td><td>${bchip(p.bucket)}</td><td>${tfmt(p.opened_at, true)}</td><td>${tfmt(p.closed_at, true)}</td>
     <td class="num">${usd(p.cost_usd)}</td><td class="num">${price(p.entry_price)}</td><td class="num">${price(p.last_price)}</td>
     <td class="num ${cls(p.pnl_usd)}">${usd(p.pnl_usd)}<br><span class="small">${pct(p.pnl_pct)}</span></td><td class="reasons">${esc(p.exit_reason)}</td>
     <td class="num">${scores(p.score_current ?? p.score, p.score_new)}</td></tr>`),
    "No closed paper trades yet.");
}

function funnel(d) {
  const lc = d.funnel.last_cycle;
  if (!lc) { $("funnel").innerHTML = `<div class="empty">Waiting for the first scan cycle…</div>`; return; }
  const ch = $("funnelChain").value;
  const f = ch ? (lc.per_chain || {})[ch] || {evaluated: 0, passed: 0, rejected: {}, pending: {}} : lc.funnel;
  const total = f.evaluated || 1, L = d.funnel.stage_labels;
  $("funnelnote").textContent = `(cycle #${lc.id}, ${tfmt(lc.finished)})`;
  let html = `<div class="funnel-row"><span>Tokens checked</span><div class="funnel-bar"><span style="width:100%"></span></div><span class="num">${f.evaluated}</span></div>`;
  for (const s of d.funnel.stages) {
    const drop = (f.rejected || {})[s] || 0, pend = (f.pending || {})[s] || 0;
    if (!drop && !pend) continue;
    html += `<div class="funnel-row"><span class="muted">${esc(L[s] || s)}</span><div class="funnel-bar"><span class="${drop ? "drop" : "pend"}" style="width:${Math.max(0.5, (drop + pend) / total * 100)}%"></span></div>
      <span class="num">${drop ? `<span class="neg">−${drop}</span>` : ""}${pend ? ` <span class="warn">⏸${pend}</span>` : ""}</span></div>`;
  }
  html += `<div class="funnel-row"><span><b>Passed → paper buy</b></span><div class="funnel-bar"><span class="pass" style="width:${Math.max(0.5, f.passed / total * 100)}%"></span></div><span class="num pos">${f.passed}</span></div>`;
  html += `<div class="muted small" style="margin-top:8px">−N = rejected at that step · ⏸N = waiting (too young, awaiting data or API budget), re-checked next cycle. Discovered ${lc.discovered} list entries this cycle (${lc.new_tokens} new tokens).</div>`;
  $("funnel").innerHTML = html;
}

function rules(d) {
  const c = d.config, N = c.buckets.new, E = c.buckets.established, TN = c.timing.new, TE = c.timing.established;
  $("rules").innerHTML = `
  <p><b>1. Market data</b> (DexScreener): liquidity, volume, market cap and real buys <i>and</i> sells. New coins need ≥ ${big(N.min_liquidity_usd)} liquidity; established coins ≥ ${big(E.min_liquidity_usd)}, market cap ≥ ${big(E.min_market_cap_usd)}, ≥ ${E.min_holders} holders.</p>
  <p><b>2. Safety</b>: Solana — mint &amp; freeze authority revoked (on-chain), no Token-2022 traps. BSC / Robinhood — GoPlus + honeypot.is: not a honeypot, buy/sell tax ≤ ${c.safety.max_buy_tax_pct}%/${c.safety.max_sell_tax_pct}%, no mint / proxy / blacklist / pause / hidden-owner powers. Biggest wallet ≤ ${N.max_top_holder_pct}% (new) or ${E.max_top_holder_pct}% (established), pools &amp; burn addresses excluded.</p>
  <p><b>3. Score</b> — <b>Current scoring</b> account: rule score ≥ ${N.min_score_to_buy} (liquidity, turnover, buy/sell balance, holder growth, concentration, age, momentum).
  <b>New scoring</b> account: model score (scoring_v1, probability × 100) ≥ ${(c.scoring_new || {}).min_score_to_buy ?? "—"}. A coin that passes either score still has to pass step 4.</p>
  <p><b>4. Good-entry timing</b> (price candles): <b>new</b> coins — ${TN.pullback_min_pct}–${TN.pullback_max_pct}% dip from the ${TN.lookback_candles * TN.candle_minutes / 60}h high after a ≥ ${TN.min_rise_before_pct}% run-up, bounced ≥ ${TN.bounce_min_pct}%, not a 5-min spike (≤ +${TN.max_m5_change_pct}%), buys ≥ sells.
  <b>established</b> coins — ${TE.pullback_min_pct}–${TE.pullback_max_pct}% pullback from the ${TE.lookback_candles}h high while price is above a rising ${TE.trend_ma_candles}h average, bounced ≥ ${TE.bounce_min_pct}%, volume recovering, not in freefall.</p>
  <p><b>5. Exits</b>: new — TP ${N.take_profit_pct}% (sell half) / ${N.take_profit2_pct}%, stop −${N.stop_loss_pct}%, trailing ${N.trailing_stop_pct}%, max ${N.max_hold_hours}h. established — TP ${E.take_profit_pct}% / ${E.take_profit2_pct}%, stop −${E.stop_loss_pct}%, trailing ${E.trailing_stop_pct}%, max ${E.max_hold_hours / 24} days.</p>
  <p><b>Money</b>: ${c.paper.position_pct_of_balance}% of equity per trade (≤ ${c.paper.max_pct_of_pool_liquidity}% of pool liquidity), max ${c.paper.max_open_positions} open, daily loss cap ${c.paper.daily_loss_cap_pct}%, ${c.paper.reentry_cooldown_hours}h re-entry cooldown.</p>`;
}

const resPill = (r) => r === "pass" ? `<span class="pill ok">PASS</span>` : r.startsWith("reject") ? `<span class="pill bad">${r.includes("final") ? "REJECT·final" : "REJECT"}</span>` : `<span class="pill wait">pending</span>`;

function feeds(d) {
  const ch = $("feedChain").value, bk = $("feedBucket").value, hideP = $("hidePending").checked, hideL = $("hideLiq").checked;
  const rows = d.feed.filter(e => (!ch || e.chain === ch) && (!bk || e.bucket === bk) && !(hideP && e.result === "pending") && !(hideL && (e.stage === "liquidity" || e.stage === "data")));
  $("feednote").textContent = `(latest cycle: ${d.feed.length} tokens, showing ${rows.length})`;
  const L = d.funnel.stage_labels;
  table("feed", ["Token", "Chain", "Bucket", "Result", "Stage", "#Liq", "#Vol 24h", "#MCap", "#Age", "#Score", "#New score", "Reasons"], rows.map(e => {
    const m = e.metrics || {};
    return `<tr><td>${tok(e.symbol, e.url, e.token)}</td><td>${chip(e.chain)}</td><td>${bchip(e.bucket)}</td><td>${resPill(e.result)}</td><td class="small">${esc(L[e.stage] || e.stage)}</td>
      <td class="num">${big(m.liq)}</td><td class="num">${big(m.vol_h24)}</td><td class="num">${big(m.mcap)}</td><td class="num">${ageMin(m.age_min)}</td>
      <td class="num">${e.score ?? "—"}${gate(e.gate_current)}</td><td class="num">${sc1(e.score_new)}${gate(e.gate_new)}</td><td class="reasons small">${esc(e.reasons)}</td></tr>`;
  }), "Nothing to show with these filters.");
  table("deep", ["Time", "Token", "Chain", "Result", "Stage", "#Scores", "Reasons"], d.deep.map(e =>
    `<tr><td class="small">${tfmt(e.ts)}</td><td>${tok(e.symbol, e.url, e.token)} ${bchip(e.bucket)}</td><td>${chip(e.chain)}</td><td>${resPill(e.result)}</td>
     <td class="num">${scores(e.score, e.score_new)}</td>
     <td class="small">${esc(L[e.stage] || e.stage)}</td><td class="reasons small">${esc(e.reasons)}</td></tr>`), "No token has reached the safety checks in the last 6 hours.");
  table("decisions", ["Time", "Kind", "Account", "Chain", "Token", "Message"], d.decisions.map(x =>
    `<tr><td class="small">${tfmt(x.ts, true)}</td><td><span class="pill ${x.kind === "BUY" ? "ok" : x.kind === "SELL" ? "" : "wait"}">${esc(x.kind)}</span></td>
     <td class="small">${x.scoring ? esc(LABEL[x.scoring] || x.scoring) : '<span class="muted">both</span>'}</td><td>${chip(x.chain)}</td>
     <td>${esc(x.symbol || "")}</td><td class="reasons small">${esc(x.message)}</td></tr>`), "No decisions yet (buys, sells, skips and pauses appear here).");
  table("cycles", ["#", "Finished", "#Found", "#New", "#Checked", "#Passed", "API calls"], d.cycles.map(c =>
    `<tr><td>${c.id}</td><td>${tfmt(c.finished)}</td><td class="num">${c.discovered}</td><td class="num">${c.new_tokens}</td><td class="num">${c.evaluated}</td><td class="num">${c.passed}</td>
     <td class="small muted">${esc(Object.entries(c.api_calls || {}).filter(([k, v]) => v).map(([k, v]) => k + " " + v).join(" · "))}</td></tr>`), "No cycles yet.");
}

function config(d) {
  const out = [];
  const walk = (obj, path) => {
    for (const [k, v] of Object.entries(obj)) {
      if (v && typeof v === "object" && !Array.isArray(v)) { out.push(`<div class="sect">${esc(path ? path + "." + k : k)}</div>`); walk(v, path ? path + "." + k : k); }
      else out.push(`<div class="row"><span>${esc(k)}</span><span>${esc(Array.isArray(v) ? v.join(", ") : v)}</span></div>`);
    }
  };
  walk(d.config, "");
  $("config").innerHTML = out.join("");
}

function fillSelect(id, d) {
  const s = $(id); if (s.options.length > 1) return;
  Object.entries(d.chains).filter(([n, c]) => c.enabled).forEach(([n]) => { const o = document.createElement("option"); o.value = n; o.textContent = CHAIN[n] || n; s.appendChild(o); });
}

function render(d) {
  DATA = d;
  $("updated").textContent = "Updated " + tfmt(d.generated, true);
  fillSelect("feedChain", d); fillSelect("funnelChain", d);
  compare(d); tabs(d);
  kpis(d); chainCards(d); breakdown(d); equityChart(d); positions(d); funnel(d); rules(d); feeds(d); config(d);
}

async function load() {
  for (const url of ["api/state", "data.json"]) {
    try { const r = await fetch(url, {cache: "no-store"}); if (r.ok) { render(await r.json()); return; } } catch (e) { /* try next */ }
  }
  $("updated").textContent = "Could not load data";
}
["feedChain", "feedBucket", "hidePending", "hideLiq"].forEach(id => $(id).addEventListener("change", () => DATA && feeds(DATA)));
$("funnelChain").addEventListener("change", () => DATA && funnel(DATA));
window.addEventListener("hashchange", () => { TAB = tabOf(); if (DATA) render(DATA); });
load(); setInterval(load, 15000);
