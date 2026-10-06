#!/usr/bin/env bash
# Keeps one component alive: restarts it if it exits. Usage: supervise.sh <name> <command...>
NAME="$1"; shift
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/logs/$NAME.log"
PY="$DIR/.venv/bin/python"
mkdir -p "$DIR/logs"
. "$DIR/venv_check.sh"   # 2026-10-06: ensure_venv (self-heal of .venv, flock-guarded across the 3 supervisors)
echo $$ > "$DIR/logs/$NAME.supervisor.pid"
trap 'kill $CHILD 2>/dev/null; rm -f "$DIR/logs/$NAME.pid"; exit 0' TERM INT
VWAIT=30
while true; do
  if ! ensure_venv >> "$LOG" 2>&1; then
    # no usable venv (e.g. no network for pip): don't start, don't alert, retry with backoff (30 s .. 10 min)
    echo "=== $(date '+%F %T %Z') $NAME not started: .venv not usable, retry in ${VWAIT}s" >> "$LOG"
    CHILD=""; sleep "$VWAIT" & wait $!
    VWAIT=$(( VWAIT * 2 > 600 ? 600 : VWAIT * 2 ))
    continue
  fi
  VWAIT=30
  echo "=== $(date '+%F %T %Z') starting $NAME" >> "$LOG"
  "$@" >> "$LOG" 2>&1 &
  CHILD=$!
  echo $CHILD > "$DIR/logs/$NAME.pid"
  wait $CHILD
  CODE=$?
  echo "=== $(date '+%F %T %Z') $NAME exited (code $CODE), restarting in 10s" >> "$LOG"
  "$PY" "$DIR/notify.py" event "restart:$NAME:$(date +%s)" >/dev/null 2>&1  # wording + 10-min collapse live in notify.py
  sleep 10
done
