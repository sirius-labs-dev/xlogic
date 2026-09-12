"""Rule-based decision when LLM is unavailable (Cursor /loop + --no-llm).

Uses tools.signals output only — no external LLM call.
Still goes through risk_gate before any order.
"""

from __future__ import annotations

from typing import Any


def decide_from_signals(
    sigs: dict[str, Any],
    account: dict[str, Any],
    *,
    after_cutoff: bool = False,
) -> dict[str, Any]:
    """
    Priority:
      1) SELL if exit_signal and spot coin balance > 0
      2) BUY if entry_candidate and no meaningful spot balance (and not after cutoff)
      3) HOLD
    """
    positions = account.get("spot_positions") or {}
    # Tiny dust threshold — below this treat as flat
    dust = 1e-8

    # 1) Exits first
    for inst_id, s in sigs.items():
        if not isinstance(s, dict) or s.get("ok") is False:
            continue
        if not s.get("exit_signal"):
            continue
        bal = float(positions.get(inst_id, 0.0) or 0.0)
        if bal <= dust:
            continue
        last = float(s.get("last") or s.get("ask") or 0)
        return {
            "action": "SELL",
            "instId": inst_id,
            "entry_px": last,
            "stop_px": 0.0,
            "take_profit_px": 0.0,
            "confidence": 0.7,
            "reason": "RULE:exit_signal_1H_below_ema50",
            "llm_ok": False,
            "decision_source": "rules",
            "latency_ms": 0,
            "usage": {},
        }

    if after_cutoff:
        return {
            "action": "HOLD",
            "instId": "",
            "entry_px": 0.0,
            "stop_px": 0.0,
            "take_profit_px": 0.0,
            "confidence": 0.0,
            "reason": "RULE:AFTER_CUTOFF",
            "llm_ok": False,
            "decision_source": "rules",
            "latency_ms": 0,
            "usage": {},
        }

    # 2) Entries — prefer first entry_candidate with flat position
    for inst_id, s in sigs.items():
        if not isinstance(s, dict) or s.get("ok") is False:
            continue
        if not s.get("entry_candidate"):
            continue
        bal = float(positions.get(inst_id, 0.0) or 0.0)
        if bal > dust:
            continue
        last = float(s.get("last") or s.get("bid") or 0)
        stop = float(s.get("suggested_stop") or 0)
        target = float(s.get("suggested_target") or 0)
        if not (last > 0 and stop > 0 and target > last and stop < last):
            continue
        return {
            "action": "BUY",
            "instId": inst_id,
            "entry_px": last,
            "stop_px": stop,
            "take_profit_px": target,
            "confidence": 0.6,
            "reason": "RULE:entry_candidate_trend_up",
            "llm_ok": False,
            "decision_source": "rules",
            "latency_ms": 0,
            "usage": {},
        }

    return {
        "action": "HOLD",
        "instId": "",
        "entry_px": 0.0,
        "stop_px": 0.0,
        "take_profit_px": 0.0,
        "confidence": 0.0,
        "reason": "RULE:no_setup",
        "llm_ok": False,
        "decision_source": "rules",
        "latency_ms": 0,
        "usage": {},
    }
