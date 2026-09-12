#!/usr/bin/env bash
# One agent tick for Cursor /loop (or cron). Emirleri script verir; Cursor sadece zamanlar.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  PY="python3"
fi
MODE_ARGS=(--once)
# Default: dry-run until you pass --live
if [[ "${1:-}" == "--live" ]]; then
  shift
else
  MODE_ARGS+=(--dry-run)
fi
# Prefer rules when no working LLM key (override with DECISION_MODE in config / env)
export TRADING_DECISION_MODE="${TRADING_DECISION_MODE:-rules}"
exec "$PY" -m agent.main "${MODE_ARGS[@]}" --decision "${TRADING_DECISION_MODE}" "$@"
