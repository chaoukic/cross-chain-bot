#!/usr/bin/env bash
DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="$(cat "$DIR/logs/port" 2>/dev/null || echo 8797)"
echo "== Crosschain status ($(date '+%F %T %Z')) — PAPER TRADING ONLY"
for n in bot web telegram; do
  sp="$DIR/logs/$n.supervisor.pid"; cp="$DIR/logs/$n.pid"
  if [ -f "$sp" ] && kill -0 "$(cat "$sp")" 2>/dev/null; then
    c=$( [ -f "$cp" ] && cat "$cp" ); echo "  $n: RUNNING (supervisor pid $(cat "$sp"), process pid ${c:-?})"
  else echo "  $n: STOPPED"; fi
done
code=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:$PORT/" 2>/dev/null)
echo "  local dashboard: http://localhost:$PORT  (HTTP $code)"
[ -f "$DIR/site/data.json" ] && echo "  site/data.json: updated $(( $(date +%s) - $(stat -c %Y "$DIR/site/data.json") ))s ago"
"$DIR/.venv/bin/python" - <<'PY' 2>/dev/null
import sys, json, time; sys.path.insert(0, "/workspace/crosschain")
from common import db, get_state, load_config
c = db(); cfg = load_config()
r = c.execute("SELECT id, finished, evaluated, passed, per_chain FROM cycles WHERE finished IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
if r:
    print(f"  last scan cycle #{r['id']} {int(time.time()-r['finished'])}s ago: checked {r['evaluated']}, passed {r['passed']}")
    for ch, v in (json.loads(r["per_chain"] or "{}")).items():
        print(f"    {ch:10s} checked {v['evaluated']:4d} (new {v['by_bucket'].get('new',0)}, established {v['by_bucket'].get('established',0)}), "
              f"safety lookups {v.get('safety_checked',0)}, passed {v['passed']}, watchlist {v.get('watchlist')}")
for ch, cc in cfg["chains"].items():
    if not cc.get("enabled"): print(f"    {ch}: DISABLED in config.toml")
mp = get_state(c, "manual_pause") or {}
print("  new entries:", "PAUSED via Telegram /pause" if mp.get("on") else ("paused by daily loss cap" if get_state(c, "paused_today", False) else "allowed"))
tg = (get_state(c, "tg_status") or {}).get("state")
print("  telegram:", {"waiting_for_token": "sending disabled (no CROSSCHAIN_TELEGRAM_TOKEN yet)", "waiting_for_start": "token found; press Start in the new bot",
                      "connected": "connected", "disabled": "disabled in config"}.get(tg, tg),
      "| queued alerts:", c.execute("SELECT COUNT(*) FROM outbox WHERE status='pending'").fetchone()[0])
print("  open paper positions:", c.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0],
      "| closed:", c.execute("SELECT COUNT(*) FROM positions WHERE status='closed'").fetchone()[0],
      "| fake cash: $%.2f" % (get_state(c, "cash", 0) or 0))
for acct in ("current", "new"):   # side-by-side scoring test (Oct 3)
    k = "cash" if acct == "current" else acct + ":cash"
    if get_state(c, k) is None:
        print(f"    {acct} scoring account: not started"); continue
    print(f"    {acct} scoring account: open", c.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND scoring=?", (acct,)).fetchone()[0],
          "| closed", c.execute("SELECT COUNT(*) FROM positions WHERE status='closed' AND scoring=?", (acct,)).fetchone()[0],
          "| fake cash $%.2f" % (get_state(c, k, 0) or 0))
ns = get_state(c, "scoring_new_status") or {}
print("  new scoring self-test:", ns.get("msg", "not run yet"))
PY
