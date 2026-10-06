#!/usr/bin/env bash
# Start everything in the background (survives logout). Safe to run again: restarts cleanly.
# PAPER TRADING ONLY. Does not touch memebot or copytrader.
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"
mkdir -p logs site
PORT="${CROSSCHAIN_PORT:-8797}"
echo "$PORT" > logs/port
# 2026-10-06 self-heal: recreate .venv (python3 -m venv + pip install -r requirements.txt) if it vanished / is broken.
. "$DIR/venv_check.sh"
ensure_venv || echo "WARNING: .venv is not usable yet (see output above); the supervisors keep retrying the rebuild."
QUIET_STOP=1 "$DIR/stop.sh" >/dev/null 2>&1
PY="$DIR/.venv/bin/python"
setsid nohup "$DIR/supervise.sh" bot "$PY" "$DIR/bot.py" >/dev/null 2>&1 < /dev/null &
setsid nohup "$DIR/supervise.sh" web "$PY" -m uvicorn web:app --host 127.0.0.1 --port "$PORT" --app-dir "$DIR" --log-level warning >/dev/null 2>&1 < /dev/null &
setsid nohup "$DIR/supervise.sh" telegram "$PY" "$DIR/telegram_bot.py" >/dev/null 2>&1 < /dev/null &
"$PY" "$DIR/notify.py" event "start:$(date +%s)" >/dev/null 2>&1  # at most one "started" alert per 10 min
sleep 5
"$DIR/status.sh"
