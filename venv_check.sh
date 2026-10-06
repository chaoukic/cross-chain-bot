#!/usr/bin/env bash
# Self-heal for the Python venv (2026-10-06). Sourced by run.sh and supervise.sh; can also be run directly:
#   ./venv_check.sh                      check/repair /workspace/crosschain/.venv
#   VENV=/tmp/cc_venv_test ./venv_check.sh   same against another path (used for testing, leaves the live venv alone)
# The box was moved/restarted and .venv vanished, so supervise.sh crash-looped on ".venv/bin/python: No such file".
# ensure_venv: OK if $VENV/bin/python exists and can import requests, fastapi, uvicorn. Otherwise rebuild with
#   python3 -m venv $VENV && $VENV/bin/pip install -q -r requirements.txt
# under flock on "$VENV.lock", so the 3 supervisors (and run.sh) never rebuild at the same time; whoever waited
# re-checks after getting the lock and normally finds it already repaired. Never sends alerts. Returns 0 = usable.
CC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_IMPORTS="import requests, fastapi, uvicorn"

venv_ok() {
  [ -x "$1/bin/python" ] && "$1/bin/python" -c "$VENV_IMPORTS" >/dev/null 2>&1
}

ensure_venv() {
  local v="${VENV:-$CC_DIR/.venv}"
  local reqs="${VENV_REQS:-$CC_DIR/requirements.txt}"
  local lock="${VENV_LOCK:-$v.lock}"
  venv_ok "$v" && return 0
  mkdir -p "$(dirname "$lock")" "$(dirname "$v")" || return 1
  (
    if ! flock -w "${VENV_LOCK_WAIT:-900}" 9; then
      echo "=== $(date '+%F %T %Z') venv: could not get $lock within ${VENV_LOCK_WAIT:-900}s (another rebuild still running?)"
      exit 1
    fi
    venv_ok "$v" && { echo "=== $(date '+%F %T %Z') venv: $v already repaired by another process"; exit 0; }
    echo "=== $(date '+%F %T %Z') venv: $v missing or broken (no bin/python or missing requests/fastapi/uvicorn) - rebuilding"
    if [ ! -x "$v/bin/python" ] || ! "$v/bin/python" -c "import sys" >/dev/null 2>&1; then
      python3 -m venv --clear "$v" || { echo "=== venv: 'python3 -m venv $v' FAILED"; exit 1; }
    fi
    if [ -f "$reqs" ]; then
      "$v/bin/pip" install -q --disable-pip-version-check -r "$reqs" || { echo "=== venv: pip install -r $reqs FAILED"; exit 1; }
    else
      "$v/bin/pip" install -q --disable-pip-version-check requests fastapi uvicorn || { echo "=== venv: pip install FAILED"; exit 1; }
    fi
    if venv_ok "$v"; then echo "=== $(date '+%F %T %Z') venv: rebuilt OK ($("$v/bin/python" --version 2>&1))"; exit 0; fi
    echo "=== $(date '+%F %T %Z') venv: rebuilt but imports still fail"; exit 1
  ) 9>"$lock"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then   # executed, not sourced
  ensure_venv && echo "venv OK: ${VENV:-$CC_DIR/.venv}"
fi
