"""Unit tests for tools.risk_gate — one case per reject code + sizing invariants."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

import config
from tools import risk_gate


REF_INSTRUMENTS = {
    "BTC-USDT": {
        "minSz": "0.00001",
        "lotSz": "0.00000001",
        "tickSz": "0.1",
    },
    "ETH-USDT": {
        "minSz": "0.0001",
        "lotSz": "0.000001",
        "tickSz": "0.01",
    },
    "BTC-TRY": {
        "minSz": "0.0001",
        "lotSz": "0.000000001",
        "tickSz": "1",
    },
}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Fresh state file + instrument cache for every test."""
    state_file = tmp_path / "state.json"
    monkeypatch.setattr(risk_gate, "STATE_PATH", state_file)
    risk_gate.clear_instrument_cache()

    def fake_get_instrument(inst_id: str):
        row = REF_INSTRUMENTS.get(inst_id)
        if not row:
            return {"ok": False, "error": f"unknown {inst_id}"}
        return {"ok": True, "data": [{**row, "instId": inst_id, "instType": "SPOT"}]}

    monkeypatch.setattr(risk_gate.okx_client, "get_instrument", fake_get_instrument)
    yield
    risk_gate.clear_instrument_cache()


def base_state(**overrides):
    state = {
        "equity": 10_000.0,
        "day_start_equity": 10_000.0,
        "available_balance": 10_000.0,
        "kill_switch": False,
        "order_timestamps": [],
        "positions": {},
        "now": "2026-09-12T12:00:00",
    }
    state.update(overrides)
    return state


def base_buy(**overrides):
    order = {
        "instId": "BTC-USDT",
        "side": "buy",
        "entry_px": 100_000.0,
        "stop_px": 99_000.0,
        "take_profit_px": 103_000.0,
        "bid": 99_999.0,
        "ask": 100_001.0,  # ~0.2 bps
    }
    order.update(overrides)
    return order


# --- reject codes ---


def test_pair_not_allowed():
    r = risk_gate.check(base_buy(instId="SOL-USDT"), base_state())
    assert r["allowed"] is False
    assert r["reason"] == "PAIR_NOT_ALLOWED"


def test_daily_loss_kill_triggers_and_persists(tmp_path):
    state = base_state(equity=9_700.0, day_start_equity=10_000.0)  # -3%
    r = risk_gate.check(base_buy(), state)
    assert r["reason"] == "DAILY_LOSS_KILL"
    assert state["kill_switch"] is True
    disk = json.loads(Path(risk_gate.STATE_PATH).read_text())
    assert disk["kill_switch"] is True


def test_kill_switch_sticky_on_later_calls():
    state = base_state(equity=9_700.0, day_start_equity=10_000.0)
    assert risk_gate.check(base_buy(), state)["reason"] == "DAILY_LOSS_KILL"

    # Equity recovers — flag must NOT clear
    recovered = base_state(equity=10_500.0, day_start_equity=10_000.0, kill_switch=False)
    r2 = risk_gate.check(base_buy(), recovered)
    assert r2["allowed"] is False
    assert r2["reason"] == "DAILY_LOSS_KILL"


def test_kill_switch_survives_process_restart_via_disk():
    state = base_state(equity=9_700.0, day_start_equity=10_000.0)
    risk_gate.check(base_buy(), state)

    fresh = base_state(equity=10_000.0, day_start_equity=10_000.0, kill_switch=False)
    r = risk_gate.check(base_buy(), fresh)
    assert r["reason"] == "DAILY_LOSS_KILL"


def test_after_cutoff():
    r = risk_gate.check(base_buy(), base_state(now="2026-09-12T19:16:00"))
    assert r["reason"] == "AFTER_CUTOFF"


def test_after_hard_stop():
    r = risk_gate.check(base_buy(), base_state(now="2026-09-12T19:20:00"))
    assert r["reason"] == "AFTER_CUTOFF"


def test_sell_allowed_after_cutoff():
    order = base_buy(side="sell", size=0.01)
    r = risk_gate.check(order, base_state(now="2026-09-12T19:18:00"))
    assert r["allowed"] is True
    assert r["reason"] == "OK"


def test_no_stop():
    r = risk_gate.check(base_buy(stop_px=None), base_state())
    assert r["reason"] == "NO_STOP"


def test_invalid_stop_ge_entry():
    r = risk_gate.check(base_buy(stop_px=100_000.0), base_state())
    assert r["reason"] == "INVALID_STOP"


def test_invalid_stop_above_entry():
    r = risk_gate.check(base_buy(stop_px=100_500.0), base_state())
    assert r["reason"] == "INVALID_STOP"


def test_invalid_target_le_entry():
    r = risk_gate.check(base_buy(take_profit_px=100_000.0), base_state())
    assert r["reason"] == "INVALID_TARGET"


def test_invalid_target_below_entry():
    r = risk_gate.check(base_buy(take_profit_px=90_000.0), base_state())
    assert r["reason"] == "INVALID_TARGET"


def test_spread_too_wide():
    # mid=100000, spread=20 bps > 10
    r = risk_gate.check(
        base_buy(bid=99_900.0, ask=100_100.0),
        base_state(),
    )
    assert r["reason"] == "SPREAD_TOO_WIDE"


def test_rate_limit():
    now = "2026-09-12T12:00:00"
    now_ts = risk_gate._now({"now": now}).timestamp()
    stamps = [now_ts - 60 * i for i in range(config.MAX_ORDERS_PER_HOUR)]
    state = base_state(now=now, order_timestamps=stamps)
    r = risk_gate.check(base_buy(), state)
    assert r["reason"] == "RATE_LIMIT"


def test_over_risk_per_trade():
    # Wide stop: minSz * distance > equity * 0.5%
    # minSz=1e-5, equity=100, max_risk=0.5; need distance > 0.5/1e-5 = 50000
    r = risk_gate.check(
        base_buy(entry_px=100_000.0, stop_px=40_000.0, take_profit_px=110_000.0),
        base_state(equity=100.0, day_start_equity=100.0, available_balance=100.0),
    )
    assert r["reason"] == "OVER_RISK_PER_TRADE"


def test_over_position():
    # Already at 30% of 10k = 3000 in BTC
    state = base_state(positions={"BTC-USDT": {"notional": 3_000.0}})
    r = risk_gate.check(base_buy(), state)
    assert r["reason"] == "OVER_POSITION"


def test_over_exposure():
    # Total exposure already at 60%
    state = base_state(
        positions={
            "BTC-USDT": {"notional": 1_000.0},
            "ETH-USDT": {"notional": 5_000.0},
        }
    )
    r = risk_gate.check(base_buy(), state)
    assert r["reason"] == "OVER_EXPOSURE"


def test_below_min_size():
    # Tiny leftover position room -> size 0.000005 < minSz 0.00001
    state = base_state(positions={"BTC-USDT": {"notional": 2_999.5}})
    r = risk_gate.check(base_buy(), state)
    assert r["reason"] == "BELOW_MIN_SIZE"


def test_insufficient_balance():
    # Enough equity for risk math, but cash too low for notional
    state = base_state(available_balance=0.01)
    r = risk_gate.check(base_buy(), state)
    assert r["reason"] == "INSUFFICIENT_BALANCE"


# --- happy path + sizing invariants ---


def test_ok_happy_path():
    r = risk_gate.check(base_buy(), base_state())
    assert r["allowed"] is True
    assert r["reason"] == "OK"
    assert r["size"] is not None and r["size"] > 0
    assert r["entry_px"] == 100_000.0
    assert r["stop_px"] == 99_000.0
    assert r["take_profit_px"] == 103_000.0


def test_risk_formula_hand_calculated():
    """
    equity=10_000, risk=0.5% -> 50 quote risk
    entry=100_000, stop=99_000 -> distance=1000
    size = 50 / 1000 = 0.05
    position cap: 0.30*10000/100000 = 0.03 -> binds
    exposure cap: 0.60*10000/100000 = 0.06
    final size before lot floor = 0.03
    lotSz=1e-8 -> 0.03 exactly
    """
    r = risk_gate.check(base_buy(), base_state())
    assert r["reason"] == "OK"
    assert r["size"] == pytest.approx(0.03)


def test_risk_formula_uncapped_by_position():
    """
    equity=10_000, distance=2000 (entry 100k stop 98k)
    risk size = 50/2000 = 0.025
    position cap = 0.03 -> risk size binds
    """
    r = risk_gate.check(
        base_buy(stop_px=98_000.0, take_profit_px=106_000.0),
        base_state(),
    )
    assert r["reason"] == "OK"
    assert r["size"] == pytest.approx(0.025)


def test_floor_never_rounds_up():
    # 0.000010009 -> lot 1e-8 floor -> 0.00001 (not up to next lot beyond raw)
    assert risk_gate.floor_to_step("0.000010009", "0.00000001") == Decimal("0.00001000")
    assert risk_gate.floor_to_step("100000.19", "0.1") == Decimal("100000.1")
    assert risk_gate.floor_to_step("100000.99", "0.1") == Decimal("100000.9")
    # Never exceeds original
    raw = Decimal("0.000019999")
    stepped = risk_gate.floor_to_step(raw, "0.00000001")
    assert stepped <= raw


def test_size_floored_not_rounded_up(monkeypatch):
    """
    Force a size that is not on a lot boundary and confirm floor.
    equity large, distance small -> size clipped by position then floored.
    Use odd tick-aligned prices so risk size is messy.
    """
    # entry 100000.0 stop 99999.9 -> distance 0.1
    # risk quote = 50, size = 50/0.1 = 500, position cap 0.03 binds
    r = risk_gate.check(
        base_buy(entry_px=100_000.0, stop_px=99_999.9, take_profit_px=100_100.0),
        base_state(),
    )
    assert r["allowed"] is True
    # 0.03 is already on lot; inject fractional room via monkeypatch on floor path
    # by using ETH lot and a computed size that needs flooring:
    order = base_buy(
        instId="ETH-USDT",
        entry_px=3_000.0,
        stop_px=2_990.0,
        take_profit_px=3_030.0,
        bid=2_999.9,
        ask=3_000.1,
    )
    # risk size = 50/10 = 5; pos cap = 3000/3000 = 1.0 -> size 1.0 exact
    r2 = risk_gate.check(order, base_state())
    assert r2["size"] == pytest.approx(1.0)

    # Make size require floor: position room notional leaves 1.0000009 ETH worth
    # remaining quote room = 3000.0027 -> /3000 = 1.0000009 -> floor lot 1e-6 -> 1.000000
    state = base_state(
        positions={"ETH-USDT": {"notional": 0.0}},
    )
    # Patch MAX so risk size is huge and position room is fractional
    monkeypatch.setattr(config, "MAX_POSITION_PCT", 0.30000027)
    # max_pos = 3000.0027, size max = 1.0000009
    r3 = risk_gate.check(order, state)
    assert r3["allowed"] is True
    assert r3["size"] == 1.0  # floored from 1.0000009
    assert r3["size"] <= 1.0000009


def test_instrument_cache_used(monkeypatch):
    calls = {"n": 0}
    real = risk_gate.okx_client.get_instrument

    def counting(inst_id):
        calls["n"] += 1
        return real(inst_id)

    monkeypatch.setattr(risk_gate.okx_client, "get_instrument", counting)
    risk_gate.clear_instrument_cache()
    risk_gate.check(base_buy(), base_state())
    risk_gate.check(base_buy(), base_state())
    assert calls["n"] == 1


def test_rule_order_kill_before_pair():
    """Kill switch must win even if pair is also invalid."""
    state = base_state(equity=9_000.0, day_start_equity=10_000.0)
    r = risk_gate.check(base_buy(instId="SOL-USDT"), state)
    assert r["reason"] == "DAILY_LOSS_KILL"
