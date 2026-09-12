"""Unit tests for tools.llm — OpenAI mocked, no network."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openai import APITimeoutError, AuthenticationError

import tools.llm as llm


def _completion(parsed=None, content=None, usage=True):
    msg = SimpleNamespace(parsed=parsed, content=content, refusal=None)
    choice = SimpleNamespace(message=msg)
    u = SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30) if usage else None
    return SimpleNamespace(choices=[choice], usage=u)


def _parsed(**kwargs):
    from tools.llm import DecisionSchema

    base = {
        "action": "HOLD",
        "instId": "BTC-USDT",
        "entry_px": 0,
        "stop_px": 0,
        "take_profit_px": 0,
        "confidence": 0.5,
        "reason": "wait",
    }
    base.update(kwargs)
    return DecisionSchema(**base)


@pytest.fixture(autouse=True)
def _env_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    yield


def test_bad_json_holds(monkeypatch):
    client = MagicMock()
    client.chat.completions.parse.return_value = _completion(
        parsed=None, content="```not json```"
    )
    monkeypatch.setattr(llm, "_client", lambda: client)
    r = llm.ask_decision({}, {})
    assert r["action"] == "HOLD"
    assert r["llm_ok"] is False
    assert r["reason"] == "LLM_BAD_JSON"


def test_buy_with_invalid_stop_holds(monkeypatch):
    client = MagicMock()
    client.chat.completions.parse.return_value = _completion(
        parsed=_parsed(
            action="BUY",
            instId="BTC-USDT",
            entry_px=100.0,
            stop_px=110.0,  # invalid: stop > entry
            take_profit_px=120.0,
            confidence=0.9,
            reason="bad stop",
        )
    )
    monkeypatch.setattr(llm, "_client", lambda: client)
    r = llm.ask_decision({}, {})
    assert r["action"] == "HOLD"
    assert r["llm_ok"] is False
    assert r["reason"] == "LLM_BAD_SCHEMA"


def test_disallowed_pair_holds(monkeypatch):
    client = MagicMock()
    client.chat.completions.parse.return_value = _completion(
        parsed=_parsed(
            action="BUY",
            instId="SOL-USDT",
            entry_px=100.0,
            stop_px=90.0,
            take_profit_px=120.0,
            confidence=0.8,
            reason="sol",
        )
    )
    monkeypatch.setattr(llm, "_client", lambda: client)
    r = llm.ask_decision({}, {})
    assert r["action"] == "HOLD"
    assert r["llm_ok"] is False
    assert r["reason"] == "LLM_BAD_SCHEMA"


def test_timeout_holds(monkeypatch):
    client = MagicMock()
    client.chat.completions.parse.side_effect = APITimeoutError(request=MagicMock())
    monkeypatch.setattr(llm, "_client", lambda: client)
    r = llm.ask_decision({}, {})
    assert r["action"] == "HOLD"
    assert r["reason"] == "LLM_TIMEOUT"
    assert r["llm_ok"] is False


def test_valid_buy_passes(monkeypatch):
    client = MagicMock()
    client.chat.completions.parse.return_value = _completion(
        parsed=_parsed(
            action="BUY",
            instId="BTC-USDT",
            entry_px=100_000.0,
            stop_px=99_000.0,
            take_profit_px=103_000.0,
            confidence=0.7,
            reason="ema up",
        )
    )
    monkeypatch.setattr(llm, "_client", lambda: client)
    r = llm.ask_decision({"BTC-USDT": {"trend": "UP"}}, {"equity": 10000})
    assert r["llm_ok"] is True
    assert r["action"] == "BUY"
    assert r["instId"] == "BTC-USDT"
    assert r["entry_px"] == 100_000.0
    assert r["stop_px"] == 99_000.0
    assert r["take_profit_px"] == 103_000.0
    assert r["confidence"] == 0.7
    assert "usage" in r
    assert "size" not in r


def test_size_field_stripped(monkeypatch):
    client = MagicMock()
    # Simulate raw content JSON with a size field
    payload = {
        "action": "HOLD",
        "instId": "ETH-USDT",
        "entry_px": 0,
        "stop_px": 0,
        "take_profit_px": 0,
        "confidence": 0.2,
        "reason": "cash",
        "size": 999,
    }
    import json

    client.chat.completions.parse.return_value = _completion(
        parsed=None, content=json.dumps(payload)
    )
    monkeypatch.setattr(llm, "_client", lambda: client)
    r = llm.ask_decision({}, {})
    assert r["action"] == "HOLD"
    assert r["llm_ok"] is True
    assert "size" not in r


def test_auth_holds(monkeypatch):
    client = MagicMock()
    err = AuthenticationError(
        message="bad key",
        response=MagicMock(status_code=401, headers={}),
        body=None,
    )
    client.chat.completions.parse.side_effect = err
    monkeypatch.setattr(llm, "_client", lambda: client)
    r = llm.ask_decision({}, {})
    assert r["action"] == "HOLD"
    assert r["reason"] == "LLM_AUTH"
