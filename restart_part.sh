#!/usr/bin/env bash
# Restart one component (bot | web | telegram) on purpose, without a "crashed" Telegram alert.
# The supervisor notices the exit and starts it again within ~10 s.
DIR="$(cd "$(dirname "$0")" && pwd)"
for n in "$@"; do
  touch "$DIR/logs/planned_restart.$n"
  [ -f "$DIR/logs/$n.pid" ] && kill "$(cat "$DIR/logs/$n.pid")" 2>/dev/null && echo "restarting $n"
done
