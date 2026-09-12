"""Risk gate — every LLM-proposed order must pass check() before placement."""

from __future__ import annotations

import json
import os
from datetime import datetime, time
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

import config
from tools import okx_client

STATE_PATH = Path(__file__).resolve().parent.parent / "logs" / "state.json"

# process-local instrument cache: instId -> {minSz, lotSz, tickSz}
_INSTRUMENT_CACHE: dict[str, dict[str, Decimal]] = {}

_DENY_FIELDS = {
    "allowed": False,
    "size": None,
    "entry_px": None,
    "stop_px": None,
    "take_profit_px": None,
}


def _deny(reason: str) -> dict[str, Any]:
    return {**_DENY_FIELDS, "reason": reason}


def _allow(
    size: float | None,
    entry_px: float | None,
    stop_px: float | None,
    take_profit_px: float | None,
) -> dict[str, Any]:
    return {
        "allowed": True,
        "reason": "OK",
        "size": size,
        "entry_px": entry_px,
        "stop_px": stop_px,
        "take_profit_px": take_profit_px,
    }


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def floor_to_step(value: Any, step: Any) -> Decimal:
    """Round down to a multiple of step. Never rounds up."""
    v = _dec(value)
    s = _dec(step)
    if s <= 0:
        raise ValueError(f"invalid step: {step}")
    return (v / s).to_integral_value(rounding=ROUND_DOWN) * s


def load_persisted_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_persisted_state(patch: dict[str, Any]) -> None:
    """Merge patch into logs/state.json (kill switch must survive restarts)."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    current = load_persisted_state()
    current.update(patch)
    tmp = STATE_PATH.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(current, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_PATH)


def _sync_kill_switch(state: dict[str, Any]) -> None:
    disk = load_persisted_state()
    if disk.get("kill_switch"):
        state["kill_switch"] = True
    if state.get("kill_switch"):
        save_persisted_state(
            {
                "kill_switch": True,
                "kill_switch_at": disk.get("kill_switch_at")
                or state.get("kill_switch_at")
                or datetime.now().isoformat(timespec="seconds"),
            }
        )


def trigger_kill_switch(state: dict[str, Any]) -> None:
    state["kill_switch"] = True
    state["kill_switch_at"] = datetime.now().isoformat(timespec="seconds")
    save_persisted_state(
        {"kill_switch": True, "kill_switch_at": state["kill_switch_at"]}
    )


def clear_instrument_cache() -> None:
    _INSTRUMENT_CACHE.clear()


def get_instrument_rules(inst_id: str) -> dict[str, Decimal] | None:
    """Fetch minSz/lotSz/tickSz via okx_client; cache for process lifetime."""
    if inst_id in _INSTRUMENT_CACHE:
        return _INSTRUMENT_CACHE[inst_id]

    result = okx_client.get_instrument(inst_id)
    if not result.get("ok"):
        return None

    data = result["data"]
    row = None
    if isinstance(data, list) and data:
        row = data[0]
    elif isinstance(data, dict):
        inner = data.get("data", data)
        if isinstance(inner, list) and inner:
            row = inner[0]
        elif isinstance(inner, dict):
            row = inner

    if not isinstance(row, dict):
        return None

    try:
        rules = {
            "minSz": _dec(row["minSz"]),
            "lotSz": _dec(row["lotSz"]),
            "tickSz": _dec(row["tickSz"]),
        }
    except (KeyError, Exception):
        return None

    _INSTRUMENT_CACHE[inst_id] = rules
    return rules


def _parse_hhmm(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _now(state: dict[str, Any]) -> datetime:
    raw = state.get("now")
    if raw is None:
        return datetime.now()
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw)
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1]
    return datetime.fromisoformat(text)


def _is_buy(order: dict[str, Any]) -> bool:
    return str(order.get("side", "buy")).lower() == "buy"


def _spread_bps(order: dict[str, Any]) -> float | None:
    if order.get("spread_bps") is not None:
        return float(order["spread_bps"])
    bid = order.get("bid")
    ask = order.get("ask")
    if bid is None or ask is None:
        return None
    bid_f = float(bid)
    ask_f = float(ask)
    mid = (bid_f + ask_f) / 2.0
    if mid <= 0:
        return None
    return (ask_f - bid_f) / mid * 10000.0


def _prune_order_timestamps(state: dict[str, Any], now_ts: float) -> list[float]:
    stamps = state.get("order_timestamps") or []
    window_start = now_ts - 3600.0
    kept: list[float] = []
    for item in stamps:
        try:
            ts = float(item)
        except (TypeError, ValueError):
            continue
        if ts >= window_start:
            kept.append(ts)
    state["order_timestamps"] = kept
    return kept


def _position_quote(state: dict[str, Any], inst_id: str) -> Decimal:
    positions = state.get("positions") or {}
    entry = positions.get(inst_id)
    if entry is None:
        return Decimal("0")
    if isinstance(entry, dict):
        if "notional" in entry:
            return _dec(entry["notional"])
        sz = _dec(entry.get("sz", 0))
        mark = _dec(entry.get("mark", entry.get("px", 0)))
        return sz * mark
    return _dec(entry)


def _total_exposure_quote(state: dict[str, Any]) -> Decimal:
    positions = state.get("positions") or {}
    total = Decimal("0")
    for inst_id in positions:
        total += _position_quote(state, inst_id)
    return total


def check(order: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """
    Validate and size an order.

    Rule order (first failure wins):
      kill switch -> pair -> cutoff -> stop/target -> spread -> rate limit -> size
    """
    _sync_kill_switch(state)

    equity = _dec(state.get("equity", 0))
    day_start = _dec(state.get("day_start_equity", equity))
    if day_start > 0:
        loss_frac = (day_start - equity) / day_start
        if loss_frac >= _dec(config.DAILY_LOSS_LIMIT):
            trigger_kill_switch(state)

    if state.get("kill_switch"):
        return _deny("DAILY_LOSS_KILL")

    inst_id = str(order.get("instId") or order.get("inst_id") or "")
    if inst_id not in config.ALLOWED_PAIRS:
        return _deny("PAIR_NOT_ALLOWED")

    now = _now(state)
    is_buy = _is_buy(order)
    if is_buy:
        cutoff = _parse_hhmm(config.CUTOFF_NEW_BUYS)
        hard = _parse_hhmm(config.HARD_STOP)
        if now.time() >= cutoff or now.time() >= hard:
            return _deny("AFTER_CUTOFF")

    entry_raw = order.get("entry_px", order.get("px"))
    stop_raw = order.get("stop_px", order.get("sl_trigger_px"))
    tp_raw = order.get("take_profit_px", order.get("tp_trigger_px"))

    if is_buy:
        if stop_raw is None or stop_raw == "":
            return _deny("NO_STOP")
        try:
            entry_px = _dec(entry_raw)
            stop_px = _dec(stop_raw)
        except Exception:
            return _deny("INVALID_STOP")
        if stop_px >= entry_px:
            return _deny("INVALID_STOP")
        if tp_raw is None or tp_raw == "":
            return _deny("INVALID_TARGET")
        try:
            take_profit_px = _dec(tp_raw)
        except Exception:
            return _deny("INVALID_TARGET")
        if take_profit_px <= entry_px:
            return _deny("INVALID_TARGET")
    else:
        # Non-buys: sizing not driven by risk formula; still require prices if present.
        try:
            entry_px = _dec(entry_raw) if entry_raw is not None else Decimal("0")
            stop_px = _dec(stop_raw) if stop_raw not in (None, "") else Decimal("0")
            take_profit_px = (
                _dec(tp_raw) if tp_raw not in (None, "") else None
            )
        except Exception:
            return _deny("INVALID_STOP")

    spread = _spread_bps(order)
    if spread is not None and spread > float(config.MAX_SPREAD_BPS):
        return _deny("SPREAD_TOO_WIDE")

    now_ts = now.timestamp()
    recent = _prune_order_timestamps(state, now_ts)
    if len(recent) >= int(config.MAX_ORDERS_PER_HOUR):
        return _deny("RATE_LIMIT")

    if not is_buy:
        # Closing / sells: no new risk sizing; pass through after schedule checks.
        size_raw = order.get("size", order.get("sz"))
        size_f = float(size_raw) if size_raw is not None else 0.0
        return _allow(
            size=size_f,
            entry_px=float(entry_px) if entry_raw is not None else None,
            stop_px=float(stop_px) if stop_raw not in (None, "") else None,
            take_profit_px=float(take_profit_px) if take_profit_px is not None else None,
        )

    # --- size calculation (LLM does not choose size) ---
    rules = get_instrument_rules(inst_id)
    if rules is None:
        return _deny("BELOW_MIN_SIZE")

    risk_distance = entry_px - stop_px
    max_risk_quote = equity * _dec(config.MAX_RISK_PER_TRADE)

    # Even the exchange minimum size would exceed per-trade risk.
    min_risk = rules["minSz"] * risk_distance
    if min_risk > max_risk_quote:
        return _deny("OVER_RISK_PER_TRADE")

    size = max_risk_quote / risk_distance

    max_pos_quote = equity * _dec(config.MAX_POSITION_PCT)
    pair_used = _position_quote(state, inst_id)
    pair_room = max_pos_quote - pair_used
    if pair_room <= 0:
        return _deny("OVER_POSITION")
    size = min(size, pair_room / entry_px)

    max_expo_quote = equity * _dec(config.MAX_TOTAL_EXPOSURE)
    expo_used = _total_exposure_quote(state)
    expo_room = max_expo_quote - expo_used
    if expo_room <= 0:
        return _deny("OVER_EXPOSURE")
    size = min(size, expo_room / entry_px)

    # Floor prices to tick, size to lot — always down.
    entry_px = floor_to_step(entry_px, rules["tickSz"])
    stop_px = floor_to_step(stop_px, rules["tickSz"])
    take_profit_px = floor_to_step(take_profit_px, rules["tickSz"])

    if stop_px >= entry_px:
        return _deny("INVALID_STOP")
    if take_profit_px <= entry_px:
        return _deny("INVALID_TARGET")

    # Recompute size with floored prices so risk stays within cap.
    risk_distance = entry_px - stop_px
    if risk_distance <= 0:
        return _deny("INVALID_STOP")
    size = min(size, max_risk_quote / risk_distance)
    size = min(size, pair_room / entry_px)
    size = min(size, expo_room / entry_px)
    size = floor_to_step(size, rules["lotSz"])

    if size < rules["minSz"]:
        return _deny("BELOW_MIN_SIZE")

    notional = size * entry_px
    available = _dec(
        state.get(
            "available_balance",
            state.get("available_quote", state.get("cash", equity)),
        )
    )
    if notional > available:
        return _deny("INSUFFICIENT_BALANCE")

    return _allow(
        size=float(size),
        entry_px=float(entry_px),
        stop_px=float(stop_px),
        take_profit_px=float(take_profit_px),
    )
