#!/usr/bin/env bash
# Stop all crosschain background processes (uses the pid files written by supervise.sh).
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ "${QUIET_STOP:-0}" != "1" ]; then
  "$DIR/.venv/bin/python" "$DIR/notify.py" event "stop:$(date +%s)" >/dev/null 2>&1
fi
for n in bot web telegram; do
  sp="$DIR/logs/$n.supervisor.pid"; cp="$DIR/logs/$n.pid"
  [ -f "$sp" ] && kill "$(cat "$sp")" 2>/dev/null
  [ -f "$cp" ] && kill "$(cat "$cp")" 2>/dev/null
  rm -f "$sp" "$cp"
done
sleep 1
echo "Stopped."
