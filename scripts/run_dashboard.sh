#!/usr/bin/env bash
# XLogic live demo console (read-only).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY="python3"
exec "$PY" dashboard/server.py
