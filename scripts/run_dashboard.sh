#!/usr/bin/env bash
# XLogic live demo console + pitch (read-only).
# Prefer: bash scripts/keep_ux.sh  (auto-restarts if something kills the server)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"
# Separate argv from dashboard/server.py so stray pkill patterns don't kill UX.
exec "$PY" -u "$ROOT/scripts/xlogic_ux.py"
