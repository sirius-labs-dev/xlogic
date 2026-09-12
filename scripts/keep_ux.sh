#!/usr/bin/env bash
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
LOG="$ROOT/logs/dashboard.log"
PY="$ROOT/.venv/bin/python"
mkdir -p "$ROOT/logs"
echo "$(date '+%H:%M:%S') watchdog start (no-pkill)" >>"$LOG"
while true; do
  if ! curl -sf -o /dev/null --max-time 1 "http://127.0.0.1:8787/" 2>/dev/null; then
    # only start if nothing listening; never pkill
    if ! lsof -iTCP:8787 -sTCP:LISTEN >/dev/null 2>&1; then
      "$PY" -u "$ROOT/scripts/xlogic_ux.py" >>"$LOG" 2>&1 &
      echo "$(date '+%H:%M:%S') spawn pid=$!" >>"$LOG"
    fi
    sleep 1.5
  fi
  sleep 1
done
