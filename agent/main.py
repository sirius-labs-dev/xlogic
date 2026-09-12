"""Agent main loop — observe → LLM → risk gate → order → journal.

Autonomy proof: clOrdId prefix \"agt\" (okx spot place has --aiBuilderCode but NO --tag;
see docs/okx-tools.json). Spot position = coin balance from account balance, NOT positions API.
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config
from tools import journal, llm, okx_client, risk_gate, rule_decision, signals

TZ = ZoneInfo("Europe/Istanbul")
STATE_PATH = ROOT / "logs" / "state.json"
CYCLE_SLEEP_SEC = 60
ORDER_TTL_SEC = 180  # 3 minutes
OKX_RETRIES = 3
OKX_RETRY_WAITS = (1.0, 3.0, 5.0)
MAX_CONSECUTIVE_ERRORS = 10

_shutdown = False


def _now() -> datetime:
    return datetime.now(TZ)


def _parse_hhmm(value: str):
    h, m = value.split(":")
    return int(h), int(m)


def _past_cutoff(now: datetime | None = None) -> bool:
    now = now or _now()
    h, m = _parse_hhmm(config.CUTOFF_NEW_BUYS)
    return (now.hour, now.minute) >= (h, m)


def _past_hard_stop(now: datetime | None = None) -> bool:
    now = now or _now()
    h, m = _parse_hhmm(config.HARD_STOP)
    return (now.hour, now.minute) >= (h, m)


def _make_cl_ord_id() -> str:
    """agt + unix_ms + short random suffix; max 32 chars."""
    ms = int(time.time() * 1000)
    suffix = "".join(random.choices("abcdefghijklmnopqrstuvwxyz0123456789", k=4))
    cid = f"agt{ms}{suffix}"
    return cid[:32]


def load_state() -> dict[str, Any]:
    disk = risk_gate.load_persisted_state()
    state: dict[str, Any] = {
        "kill_switch": bool(disk.get("kill_switch", False)),
        "kill_switch_at": disk.get("kill_switch_at"),
        "day_start_equity": disk.get("day_start_equity"),
        "order_timestamps": list(disk.get("order_timestamps") or []),
        "pending_orders": dict(disk.get("pending_orders") or {}),
        "cycle": int(disk.get("cycle") or 0),
        "consecutive_errors": int(disk.get("consecutive_errors") or 0),
        "hold_only": bool(disk.get("hold_only", False)),
        "cutoff_flatten_done": bool(disk.get("cutoff_flatten_done", False)),
    }
    return state


def save_state(state: dict[str, Any]) -> None:
    try:
        risk_gate.save_persisted_state(
            {
                "kill_switch": bool(state.get("kill_switch")),
                "kill_switch_at": state.get("kill_switch_at"),
                "day_start_equity": state.get("day_start_equity"),
                "order_timestamps": state.get("order_timestamps") or [],
                "pending_orders": state.get("pending_orders") or {},
                "cycle": int(state.get("cycle") or 0),
                "consecutive_errors": int(state.get("consecutive_errors") or 0),
                "hold_only": bool(state.get("hold_only", False)),
                "cutoff_flatten_done": bool(state.get("cutoff_flatten_done", False)),
            }
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] state save failed: {exc}", file=sys.stderr)


def okx_retry(fn, *args, **kwargs) -> dict[str, Any]:
    """Call okx helper with 3 retries (1s, 3s, 5s). Never raises."""
    last: dict[str, Any] = {"ok": False, "error": "unknown"}
    for attempt in range(OKX_RETRIES):
        try:
            last = fn(*args, **kwargs)
            if isinstance(last, dict) and last.get("ok"):
                return last
        except Exception as exc:  # noqa: BLE001
            last = {"ok": False, "error": str(exc)}
        if attempt < OKX_RETRIES - 1:
            time.sleep(OKX_RETRY_WAITS[attempt])
    return last if isinstance(last, dict) else {"ok": False, "error": str(last)}


def _extract_list(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get("data", data.get("details", data.get("orders")))
        if isinstance(inner, list):
            return inner
    return []


def parse_balances(balance_payload: Any) -> dict[str, float]:
    """Return {ccy: avail/eq float} from OKX balance response."""
    out: dict[str, float] = {}
    rows = _extract_list(balance_payload)
    # OKX often: [{details: [{ccy, eq, availBal, ...}], totalEq: ...}]
    details: list[Any] = []
    for row in rows:
        if isinstance(row, dict) and "details" in row:
            d = row.get("details") or []
            if isinstance(d, list):
                details.extend(d)
        elif isinstance(row, dict) and "ccy" in row:
            details.append(row)
    if not details and isinstance(balance_payload, dict):
        d = balance_payload.get("details")
        if isinstance(d, list):
            details = d

    for d in details:
        if not isinstance(d, dict):
            continue
        ccy = str(d.get("ccy") or "")
        if not ccy:
            continue
        # Prefer `eq` (includes ord-frozen) for portfolio equity / spot size.
        # `availBal` alone under-counts when TP/SL algos freeze the coin → false kill-switch.
        raw = d.get("eq", d.get("cashBal", d.get("availBal", 0)))
        try:
            amt = float(raw or 0)
        except (TypeError, ValueError):
            amt = 0.0
        if amt > 0:
            out[ccy] = out.get(ccy, 0.0) + amt
    return out


def parse_total_eq(balance_payload: Any) -> float | None:
    """OKX account-level totalEq when present (USD)."""
    rows = _extract_list(balance_payload)
    for row in rows:
        if isinstance(row, dict) and row.get("totalEq") not in (None, ""):
            try:
                return float(row["totalEq"])
            except (TypeError, ValueError):
                pass
    if isinstance(balance_payload, dict) and balance_payload.get("totalEq") not in (None, ""):
        try:
            return float(balance_payload["totalEq"])
        except (TypeError, ValueError):
            return None
    return None


def coin_from_inst(inst_id: str) -> str:
    return inst_id.split("-")[0]


def quote_from_inst(inst_id: str) -> str:
    parts = inst_id.split("-")
    return parts[1] if len(parts) > 1 else config.QUOTE_CCY


def compute_equity(
    balances: dict[str, float],
    prices: dict[str, float],
    quote_ccy: str,
) -> tuple[float, dict[str, float]]:
    breakdown = dict(balances)
    total = float(balances.get(quote_ccy, 0.0))
    for inst_id, px in prices.items():
        base = coin_from_inst(inst_id)
        amt = float(balances.get(base, 0.0))
        total += amt * float(px)
    return total, breakdown


def signals_summary(sigs: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for inst, s in sigs.items():
        if not isinstance(s, dict):
            summary[inst] = {"ok": False}
            continue
        if s.get("ok") is False or "error" in s and "last" not in s:
            summary[inst] = {"ok": False, "error": s.get("error")}
            continue
        summary[inst] = {
            "last": s.get("last"),
            "spread_bps": s.get("spread_bps"),
            "trend": s.get("trend"),
            "entry_candidate": s.get("entry_candidate"),
            "exit_signal": s.get("exit_signal"),
            "ema20": s.get("ema20"),
            "ema50": s.get("ema50"),
            "atr14": s.get("atr14"),
        }
    return summary


def build_gate_state(
    state: dict[str, Any],
    equity: float,
    balances: dict[str, float],
    prices: dict[str, float],
    quote_ccy: str,
) -> dict[str, Any]:
    positions: dict[str, Any] = {}
    for inst in config.ALLOWED_PAIRS:
        base = coin_from_inst(inst)
        amt = float(balances.get(base, 0.0))
        px = float(prices.get(inst, 0.0))
        positions[inst] = {"sz": amt, "mark": px, "notional": amt * px}

    if state.get("day_start_equity") is None:
        state["day_start_equity"] = equity

    return {
        "equity": equity,
        "day_start_equity": float(state["day_start_equity"]),
        "available_balance": float(balances.get(quote_ccy, 0.0)),
        "kill_switch": bool(state.get("kill_switch")),
        "order_timestamps": list(state.get("order_timestamps") or []),
        "positions": positions,
        "now": _now().isoformat(timespec="seconds"),
    }


def place_or_log(
    *,
    dry_run: bool,
    side: str,
    inst_id: str,
    sz: str,
    px: str,
    cl_ord_id: str,
    stop_px: str | None = None,
    tp_px: str | None = None,
) -> dict[str, Any]:
    if side.upper() == "BUY":
        cmd = (
            f"okx --json spot place --instId {inst_id} --side buy --ordType limit "
            f"--sz {sz} --px {px} --tdMode cash --tgtCcy base_ccy --clOrdId {cl_ord_id} "
            f"--slTriggerPx {stop_px} --slOrdPx -1 --tpTriggerPx {tp_px} --tpOrdPx -1"
        )
    else:
        cmd = (
            f"okx --json spot place --instId {inst_id} --side sell --ordType limit "
            f"--sz {sz} --px {px} --tdMode cash --tgtCcy base_ccy --clOrdId {cl_ord_id}"
        )

    if dry_run:
        print(f"[DRY] would place: {cmd}")
        return {
            "ok": True,
            "dry": True,
            "sent": False,
            "clOrdId": cl_ord_id,
            "cmd": cmd,
            "data": {"clOrdId": cl_ord_id, "dry": True},
        }

    if side.upper() == "BUY":
        return okx_retry(
            okx_client.place_limit_buy_with_stop,
            inst_id,
            sz,
            px,
            str(stop_px),
            str(tp_px),
            cl_ord_id,
        )
    return okx_retry(okx_client.place_limit_sell, inst_id, sz, px, cl_ord_id)


def cancel_or_log(inst_id: str, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        print(f"[DRY] would cancel all open orders for {inst_id}")
        return {"ok": True, "dry": True, "sent": False}
    return okx_retry(okx_client.cancel_all, inst_id)


def expire_stale_orders(state: dict[str, Any], dry_run: bool) -> None:
    pending = dict(state.get("pending_orders") or {})
    now_ts = time.time()
    keep: dict[str, Any] = {}
    for cl_id, meta in pending.items():
        if not isinstance(meta, dict):
            continue
        sent_at = float(meta.get("sent_at") or 0)
        inst_id = str(meta.get("instId") or "")
        if sent_at and now_ts - sent_at >= ORDER_TTL_SEC and inst_id:
            print(f"[expire] cancelling stale order {cl_id} on {inst_id}")
            if dry_run:
                print(f"[DRY] would cancel ord/clOrdId={cl_id} instId={inst_id}")
            else:
                okx_retry(okx_client.cancel_order, inst_id, None, cl_id)
            # drop from pending
            continue
        keep[cl_id] = meta
    state["pending_orders"] = keep


def flatten_all_coins(
    balances: dict[str, float],
    prices: dict[str, float],
    dry_run: bool,
    state: dict[str, Any],
) -> list[str]:
    """Cancel buys and limit-sell coin balances for allowed pairs."""
    notes: list[str] = []
    for inst in config.ALLOWED_PAIRS:
        base = coin_from_inst(inst)
        amt = float(balances.get(base, 0.0))
        cancel_or_log(inst, dry_run)
        if amt <= 0:
            continue
        # Get ask for maker-ish sell
        ticker = okx_retry(okx_client.get_ticker, inst)
        ask = None
        if ticker.get("ok"):
            rows = _extract_list(ticker.get("data"))
            if rows and isinstance(rows[0], dict):
                ask = rows[0].get("askPx") or rows[0].get("ask")
        if ask is None:
            ask = prices.get(inst)
        if ask is None:
            notes.append(f"{inst}:no_ask")
            continue
        # Floor size/price via instrument rules
        rules = risk_gate.get_instrument_rules(inst)
        if rules:
            sz = risk_gate.floor_to_step(amt, rules["lotSz"])
            px = risk_gate.floor_to_step(ask, rules["tickSz"])
            if sz < rules["minSz"]:
                notes.append(f"{inst}:below_min")
                continue
            sz_s, px_s = str(sz), str(px)
        else:
            sz_s, px_s = str(amt), str(ask)
        cl_id = _make_cl_ord_id()
        res = place_or_log(
            dry_run=dry_run, side="SELL", inst_id=inst, sz=sz_s, px=px_s, cl_ord_id=cl_id
        )
        notes.append(f"{inst}:SELL:{'ok' if res.get('ok') else 'fail'}")
        if res.get("ok"):
            state.setdefault("pending_orders", {})[cl_id] = {
                "instId": inst,
                "sent_at": time.time(),
                "side": "SELL",
            }
            state.setdefault("order_timestamps", []).append(time.time())
    return notes


def collect_account(
    dry_run: bool,
) -> tuple[dict[str, float], dict[str, float], float, dict[str, Any], list[Any]]:
    """Returns balances, prices, equity, account_for_llm, open_orders."""
    quote = config.QUOTE_CCY
    prices: dict[str, float] = {}
    for inst in config.ALLOWED_PAIRS:
        t = okx_retry(okx_client.get_ticker, inst)
        if t.get("ok"):
            rows = _extract_list(t.get("data"))
            if rows and isinstance(rows[0], dict):
                try:
                    prices[inst] = float(rows[0].get("last") or 0)
                except (TypeError, ValueError):
                    pass

    bal_res = okx_retry(okx_client.get_balance)
    balances: dict[str, float] = {}
    if bal_res.get("ok"):
        balances = parse_balances(bal_res.get("data"))
    elif dry_run:
        balances = {quote: float(config.DRY_RUN_EQUITY)}
        print(
            f"[DRY] balance unavailable ({bal_res.get('error', '')[:80]}); "
            f"using DRY_RUN_EQUITY={config.DRY_RUN_EQUITY}"
        )
    else:
        print(f"[warn] balance failed: {bal_res.get('error')}", file=sys.stderr)

    equity, breakdown = compute_equity(balances, prices, quote)
    if bal_res.get("ok"):
        total_eq = parse_total_eq(bal_res.get("data"))
        if total_eq is not None and total_eq > 0:
            equity = total_eq
    orders_res = okx_retry(okx_client.get_open_orders)
    open_orders = _extract_list(orders_res.get("data")) if orders_res.get("ok") else []

    account = {
        "equity": equity,
        "balances": balances,
        "prices": prices,
        "open_orders_count": len(open_orders),
        "quote_ccy": quote,
        # Spot: position = coin balance (NOT account positions endpoint)
        "spot_positions": {
            inst: float(balances.get(coin_from_inst(inst), 0.0))
            for inst in config.ALLOWED_PAIRS
        },
    }
    return balances, prices, equity, account, open_orders


def run_cycle(state: dict[str, Any], dry_run: bool, decision_mode: str = "auto") -> None:
    global _shutdown
    mode = "DRY" if dry_run else "LIVE"
    state["cycle"] = int(state.get("cycle") or 0) + 1
    cycle = state["cycle"]
    now = _now()
    ts_short = now.strftime("%H:%M:%S")

    # --- time gates ---
    if _past_hard_stop(now):
        print(f"[#{cycle} {ts_short}] HARD_STOP reached — shutting down")
        balances, prices, equity, _, _ = collect_account(dry_run)
        journal.log_equity(equity, {**balances, "_note": "hard_stop"})
        save_state(state)
        raise SystemExit(0)

    if _past_cutoff(now) and not state.get("cutoff_flatten_done"):
        print(f"[#{cycle} {ts_short}] CUTOFF — cancelling opens & flattening coins")
        balances, prices, equity, _, _ = collect_account(dry_run)
        notes = flatten_all_coins(balances, prices, dry_run, state)
        state["cutoff_flatten_done"] = True
        print(f"[#{cycle} {ts_short}] flatten: {', '.join(notes) or 'nothing'}")
        save_state(state)

    expire_stale_orders(state, dry_run)

    # 1. COLLECT
    balances, prices, equity, account, open_orders = collect_account(dry_run)
    if state.get("day_start_equity") is None and equity > 0:
        state["day_start_equity"] = equity

    try:
        sigs = signals.get_all_signals(list(config.ALLOWED_PAIRS))
    except Exception as exc:  # noqa: BLE001
        sigs = {p: {"ok": False, "error": str(exc)} for p in config.ALLOWED_PAIRS}

    # 2. DECIDE (LLM and/or rules — Cursor /loop uses --decision rules)
    hold_only = bool(state.get("hold_only")) or bool(state.get("kill_switch"))
    after_cutoff = _past_cutoff(now)
    decision_mode = (decision_mode or getattr(config, "DECISION_MODE", "auto")).lower()

    decision: dict[str, Any]
    if hold_only:
        decision = {
            "action": "HOLD",
            "instId": "",
            "entry_px": 0,
            "stop_px": 0,
            "take_profit_px": 0,
            "confidence": 0,
            "reason": "HOLD_ONLY_MODE",
            "llm_ok": False,
            "decision_source": "hold_only",
            "latency_ms": 0,
            "usage": {},
        }
    elif decision_mode == "rules":
        decision = rule_decision.decide_from_signals(
            sigs, account, after_cutoff=after_cutoff
        )
    else:
        # llm or auto
        try:
            decision = llm.ask_decision(sigs, account)
            decision["decision_source"] = "llm"
        except Exception as exc:  # noqa: BLE001
            decision = {
                "action": "HOLD",
                "instId": "",
                "reason": f"LLM_UNKNOWN:{exc}",
                "llm_ok": False,
                "decision_source": "llm",
                "latency_ms": 0,
                "usage": {},
                "entry_px": 0,
                "stop_px": 0,
                "take_profit_px": 0,
                "confidence": 0,
            }
        if not decision.get("llm_ok", False):
            if decision_mode == "auto":
                # Fall back to signal rules (works without API key)
                fallback = rule_decision.decide_from_signals(
                    sigs, account, after_cutoff=after_cutoff
                )
                fallback["reason"] = (
                    f"FALLBACK_AFTER_{decision.get('reason', 'LLM_FAIL')}:"
                    f"{fallback.get('reason', '')}"
                )[:200]
                fallback["llm_ok"] = False
                fallback["decision_source"] = "rules_fallback"
                fallback["usage"] = decision.get("usage") or {}
                fallback["latency_ms"] = decision.get("latency_ms") or 0
                decision = fallback
            else:
                decision = {**decision, "action": "HOLD", "decision_source": "llm"}

    action = str(decision.get("action") or "HOLD").upper()
    inst_id = str(decision.get("instId") or "")

    # Force no new buys after cutoff
    if after_cutoff and action == "BUY":
        action = "HOLD"
        decision = {**decision, "action": "HOLD", "reason": "AFTER_CUTOFF"}

    gate_result: dict[str, Any] = {
        "allowed": False,
        "reason": "SKIPPED",
        "size": None,
        "entry_px": None,
        "stop_px": None,
        "take_profit_px": None,
    }
    order_result: dict[str, Any] = {
        "sent": False,
        "ok": False,
        "ordId": None,
        "clOrdId": None,
        "detail": None,
    }
    line_bits: list[str] = []

    # 5. EXIT on trend break (before new entries)
    for inst, s in sigs.items():
        if not isinstance(s, dict) or not s.get("exit_signal"):
            continue
        base = coin_from_inst(inst)
        amt = float(balances.get(base, 0.0))
        if amt <= 0:
            continue
        print(f"[#{cycle} {ts_short}] EXIT_SIGNAL {inst} — closing spot balance {amt}")
        cancel_or_log(inst, dry_run)
        ticker = okx_retry(okx_client.get_ticker, inst)
        ask = prices.get(inst)
        if ticker.get("ok"):
            rows = _extract_list(ticker.get("data"))
            if rows and isinstance(rows[0], dict):
                ask = float(rows[0].get("askPx") or ask or 0)
        rules = risk_gate.get_instrument_rules(inst)
        if rules and ask:
            sz = risk_gate.floor_to_step(amt, rules["lotSz"])
            px = risk_gate.floor_to_step(ask, rules["tickSz"])
            if sz >= rules["minSz"]:
                cl_id = _make_cl_ord_id()
                res = place_or_log(
                    dry_run=dry_run,
                    side="SELL",
                    inst_id=inst,
                    sz=str(sz),
                    px=str(px),
                    cl_ord_id=cl_id,
                )
                line_bits.append(f"{inst} EXIT_SELL")
                if res.get("ok"):
                    state.setdefault("pending_orders", {})[cl_id] = {
                        "instId": inst,
                        "sent_at": time.time(),
                        "side": "SELL",
                    }
                    state.setdefault("order_timestamps", []).append(time.time())

    # 3–4. RISK GATE + ORDER for LLM suggestion
    if action in ("BUY", "SELL") and inst_id:
        sig = sigs.get(inst_id) if isinstance(sigs.get(inst_id), dict) else {}
        bid = sig.get("bid") if sig else None
        ask = sig.get("ask") if sig else None
        if bid is None or ask is None:
            t = okx_retry(okx_client.get_ticker, inst_id)
            if t.get("ok"):
                rows = _extract_list(t.get("data"))
                if rows and isinstance(rows[0], dict):
                    bid = float(rows[0].get("bidPx") or 0)
                    ask = float(rows[0].get("askPx") or 0)

        # Maker-ish: buy at bid, sell at ask
        entry_hint = float(decision.get("entry_px") or 0)
        if action == "BUY" and bid:
            entry_hint = float(bid)
        elif action == "SELL" and ask:
            entry_hint = float(ask)

        order = {
            "instId": inst_id,
            "side": action.lower(),
            "entry_px": entry_hint or decision.get("entry_px"),
            "stop_px": decision.get("stop_px"),
            "take_profit_px": decision.get("take_profit_px"),
            "bid": bid,
            "ask": ask,
            "spread_bps": sig.get("spread_bps") if sig else None,
            "size": None,  # risk_gate sizes buys
        }
        if action == "SELL":
            base = coin_from_inst(inst_id)
            order["size"] = float(balances.get(base, 0.0))

        gate_state = build_gate_state(state, equity, balances, prices, config.QUOTE_CCY)
        try:
            gate_result = risk_gate.check(order, gate_state)
        except Exception as exc:  # noqa: BLE001
            gate_result = {
                "allowed": False,
                "reason": f"GATE_ERROR:{exc}",
                "size": None,
                "entry_px": None,
                "stop_px": None,
                "take_profit_px": None,
            }
        # Sync kill / timestamps back
        state["kill_switch"] = bool(gate_state.get("kill_switch") or state.get("kill_switch"))
        state["order_timestamps"] = list(gate_state.get("order_timestamps") or [])

        if not gate_result.get("allowed"):
            reason = gate_result.get("reason") or "UNKNOWN"
            msg = f"GATED: {reason}"
            print(f"[#{cycle} {ts_short}] {inst_id} {msg}")
            line_bits.append(f"{inst_id} {msg}")
        else:
            cl_id = _make_cl_ord_id()
            sz = gate_result.get("size")
            px = gate_result.get("entry_px")
            sl = gate_result.get("stop_px")
            tp = gate_result.get("take_profit_px")
            if action == "BUY":
                res = place_or_log(
                    dry_run=dry_run,
                    side="BUY",
                    inst_id=inst_id,
                    sz=str(sz),
                    px=str(px),
                    cl_ord_id=cl_id,
                    stop_px=str(sl),
                    tp_px=str(tp),
                )
            else:
                res = place_or_log(
                    dry_run=dry_run,
                    side="SELL",
                    inst_id=inst_id,
                    sz=str(sz if sz is not None else order.get("size")),
                    px=str(px),
                    cl_ord_id=cl_id,
                )
            order_result = {
                "sent": not dry_run,
                "ok": bool(res.get("ok")),
                "clOrdId": cl_id,
                "ordId": None,
                "detail": res if dry_run else {"ok": res.get("ok"), "error": res.get("error")},
            }
            if res.get("ok"):
                data = res.get("data")
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    order_result["ordId"] = data[0].get("ordId")
                elif isinstance(data, dict):
                    order_result["ordId"] = data.get("ordId")
                state.setdefault("pending_orders", {})[cl_id] = {
                    "instId": inst_id,
                    "sent_at": time.time(),
                    "side": action,
                }
                state.setdefault("order_timestamps", []).append(time.time())
                line_bits.append(f"{inst_id} {action} OK clOrdId={cl_id}")
            else:
                line_bits.append(f"{inst_id} {action} FAIL")
    else:
        # HOLD path — still show per-pair status
        src = decision.get("decision_source") or "unknown"
        reason = str(decision.get("reason") or "")[:36]
        for inst in config.ALLOWED_PAIRS:
            s = sigs.get(inst) if isinstance(sigs.get(inst), dict) else {}
            if s and s.get("ok") is False:
                line_bits.append(f"{inst} SIG_ERR")
            else:
                line_bits.append(f"{inst} HOLD")
        if line_bits:
            line_bits[0] = f"{line_bits[0]} [{src}:{reason}]"

    # stdout one-liner
    if not line_bits:
        line_bits = [f"{p} HOLD" for p in config.ALLOWED_PAIRS]
    summary_line = (
        f"[#{cycle} {ts_short}] " + " | ".join(line_bits) + f" | kasa: {equity:.2f}"
    )
    print(summary_line)

    # 6. JOURNAL
    journal.log_decision(
        {
            "ts": now.isoformat(timespec="seconds"),
            "cycle": cycle,
            "mode": mode,
            "signals_summary": signals_summary(sigs),
            "llm": {
                "action": decision.get("action"),
                "instId": decision.get("instId"),
                "entry_px": decision.get("entry_px"),
                "stop_px": decision.get("stop_px"),
                "take_profit_px": decision.get("take_profit_px"),
                "confidence": decision.get("confidence"),
                "reason": decision.get("reason"),
                "llm_ok": decision.get("llm_ok"),
                "decision_source": decision.get("decision_source"),
                "latency_ms": decision.get("latency_ms"),
                "usage": decision.get("usage"),
            },
            "gate": {
                "allowed": gate_result.get("allowed"),
                "reason": gate_result.get("reason"),
                "size": gate_result.get("size"),
                "entry_px": gate_result.get("entry_px"),
                "stop_px": gate_result.get("stop_px"),
                "take_profit_px": gate_result.get("take_profit_px"),
            },
            "order": order_result,
            "equity": equity,
            "open_orders_count": len(open_orders),
        }
    )
    journal.log_equity(equity, balances)
    save_state(state)


def _handle_signal(signum, frame) -> None:  # noqa: ANN001
    global _shutdown
    _shutdown = True
    print("\nKapaniyor...", flush=True)


def graceful_shutdown(state: dict[str, Any], dry_run: bool) -> None:
    try:
        for inst in config.ALLOWED_PAIRS:
            cancel_or_log(inst, dry_run)
        balances, prices, equity, _, _ = collect_account(dry_run)
        journal.log_equity(equity, {**balances, "_note": "shutdown"})
        save_state(state)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] shutdown cleanup: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OKX spot trading agent loop")
    parser.add_argument("--dry-run", action="store_true", help="Do not send place/cancel")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument(
        "--decision",
        choices=("auto", "llm", "rules"),
        default=None,
        help="auto=LLM then rules fallback; rules=no LLM (Cursor /loop); llm=LLM only",
    )
    args = parser.parse_args(argv)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    state = load_state()
    dry_run = bool(args.dry_run)
    decision_mode = (
        args.decision
        or __import__("os").environ.get("TRADING_DECISION_MODE")
        or getattr(config, "DECISION_MODE", "auto")
    )
    print(
        f"agent start mode={'DRY' if dry_run else 'LIVE'} once={args.once} "
        f"decision={decision_mode} pairs={config.ALLOWED_PAIRS}"
    )
    print(
        "autonomy: clOrdId prefix 'agt' "
        "(docs/okx-tools.json: spot place has --aiBuilderCode, no --tag)"
    )
    print("Cursor /loop: bash scripts/loop_tick.sh  (see docs/CURSOR_LOOP.md)")

    while not _shutdown:
        try:
            run_cycle(state, dry_run, decision_mode=str(decision_mode))
            state["consecutive_errors"] = 0
            save_state(state)
        except SystemExit:
            graceful_shutdown(state, dry_run)
            raise
        except Exception as exc:  # noqa: BLE001
            state["consecutive_errors"] = int(state.get("consecutive_errors") or 0) + 1
            print(
                f"[error] cycle crashed: {exc}\n{traceback.format_exc()}",
                file=sys.stderr,
            )
            journal.log_decision(
                {
                    "ts": _now().isoformat(timespec="seconds"),
                    "cycle": state.get("cycle"),
                    "mode": "DRY" if dry_run else "LIVE",
                    "signals_summary": {},
                    "llm": {"llm_ok": False, "reason": "CYCLE_CRASH"},
                    "gate": {"allowed": False, "reason": "CYCLE_CRASH"},
                    "order": {"sent": False, "ok": False, "detail": str(exc)},
                }
            )
            if state["consecutive_errors"] >= MAX_CONSECUTIVE_ERRORS:
                print(
                    f"[CRITICAL] {MAX_CONSECUTIVE_ERRORS} consecutive errors — "
                    "cancelling orders, HOLD-only mode",
                    file=sys.stderr,
                )
                for inst in config.ALLOWED_PAIRS:
                    cancel_or_log(inst, dry_run)
                state["hold_only"] = True
            save_state(state)

        if args.once or _shutdown:
            break
        # sleep in chunks so SIGINT is responsive
        for _ in range(CYCLE_SLEEP_SEC):
            if _shutdown:
                break
            time.sleep(1)

    graceful_shutdown(state, dry_run)
    print("agent stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
