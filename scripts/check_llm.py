#!/usr/bin/env python3
"""Live + mock check for tools.llm.ask_decision — never prints secrets."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from tools.llm import ask_decision
from tools.signals import get_all_signals


def _safe_print(obj: dict) -> None:
    # Defense: scrub any accidental key-like fields
    redacted = dict(obj)
    for k in list(redacted):
        if "key" in k.lower() or "secret" in k.lower() or "token" == k.lower():
            redacted[k] = "***"
    print(json.dumps(redacted, ensure_ascii=False, indent=2))


def main() -> int:
    print("=== LIVE ask_decision (real signals) ===")
    signals = get_all_signals(["BTC-USDT", "ETH-USDT"])
    account = {"note": "dry-check", "equity_estimate": None}
    # Do not print .env or API key
    result = ask_decision(signals, account)
    _safe_print(result)

    print("\n=== MOCK bad JSON -> expect HOLD / LLM_BAD_JSON ===")
    import tools.llm as llm_mod

    bad = MagicMock()
    bad.choices = [MagicMock()]
    bad.choices[0].message.parsed = None
    bad.choices[0].message.content = "not-json{{"
    bad.usage = MagicMock(prompt_tokens=1, completion_tokens=1, total_tokens=2)

    class BoomClient:
        def __init__(self):
            self.chat = MagicMock()
            self.chat.completions.parse = MagicMock(return_value=bad)

    original = llm_mod._client
    llm_mod._client = lambda: BoomClient()  # type: ignore
    try:
        mocked = ask_decision({"BTC-USDT": {"trend": "UP"}}, {"cash": 1000})
        _safe_print(mocked)
        ok = mocked.get("action") == "HOLD" and mocked.get("reason") == "LLM_BAD_JSON"
        print("mock_ok:", ok)
    finally:
        llm_mod._client = original

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
