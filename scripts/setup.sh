#!/usr/bin/env bash
# One-shot setup so anyone can run XLogic from the console.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> XLogic setup"
echo "    repo: $ROOT"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found" >&2
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo "==> Creating .venv"
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "==> Wrote .env from .env.example — edit ANTHROPIC_API_KEY (or use --decision rules)"
else
  echo "==> .env already exists (left untouched)"
fi

OKX_PATH="${OKX_BIN:-}"
if [[ -z "$OKX_PATH" ]]; then
  OKX_PATH="$(command -v okx 2>/dev/null || true)"
fi
if [[ -z "$OKX_PATH" && -x /opt/homebrew/bin/okx ]]; then
  OKX_PATH="/opt/homebrew/bin/okx"
fi
if [[ -z "$OKX_PATH" && -x /usr/local/bin/okx ]]; then
  OKX_PATH="/usr/local/bin/okx"
fi

if [[ -n "$OKX_PATH" ]]; then
  echo "==> okx CLI: $OKX_PATH"
  "$OKX_PATH" --version 2>/dev/null || true
else
  echo "WARN: okx CLI not found on PATH."
  echo "      Install OKX CLI, then: okx auth  (site=tr for OKX TR)"
  echo "      Or set OKX_BIN=/path/to/okx in .env"
fi

echo ""
echo "Ready. Examples:"
echo "  source .venv/bin/activate"
echo "  python -m agent.main --decision rules --dry-run   # safe smoke"
echo "  python -m agent.main --decision auto              # live (needs okx auth + keys)"
echo "  bash scripts/run_dashboard.sh                     # console http://127.0.0.1:8787"
echo "  pytest -q"
