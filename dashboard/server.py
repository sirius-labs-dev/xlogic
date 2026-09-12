#!/usr/bin/env python3
"""XLogic console — read-only live demo UX for the spot agent."""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
from tools.okx_client import resolve_okx_bin  # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
LOGS = ROOT / "logs"
HOST = "127.0.0.1"
PORT = 8787


def _okx_bin() -> str:
    return resolve_okx_bin()

_CYCLE_RE = re.compile(r"\[#(\d+)\s+(\d{2}:\d{2}:\d{2})\]")
_QUOTE = str(getattr(config, "QUOTE_CCY", "USDT"))


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _tail_jsonl(path: Path, n: int = 40) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _tail_text(path: Path, n: int = 20) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()[-n:]
    except Exception:  # noqa: BLE001
        return []


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _gate_stats(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str((d.get("gate") or {}).get("reason") or "?") for d in decisions)
    gated_n = sum(
        n for reason, n in counts.items() if reason not in ("OK", "SKIPPED", "?", "")
    )
    # Prefer interesting rejects first for the bar chart
    order = [
        "BELOW_MIN_SIZE",
        "RATE_LIMIT",
        "DAILY_LOSS_KILL",
        "MAX_POSITION",
        "MAX_EXPOSURE",
        "SPREAD",
        "OK",
        "SKIPPED",
    ]
    ranked = sorted(
        counts.items(),
        key=lambda kv: (
            order.index(kv[0]) if kv[0] in order else 50,
            -kv[1],
        ),
    )
    return {
        "counts": dict(counts),
        "ranked": [{"reason": r, "n": n} for r, n in ranked],
        "gated_n": gated_n,
        "ok_n": counts.get("OK", 0),
        "skipped_n": counts.get("SKIPPED", 0),
        "total": len(decisions),
    }


def _equity_series(day_start: float | None, limit: int = 96) -> list[dict[str, Any]]:
    path = LOGS / "equity.csv"
    if not path.exists() or path.stat().st_size == 0:
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                val = _f(row.get("total_value"))
                if val is None:
                    continue
                # Drop dry-run ~1000 equity when live day_start is ~30
                if day_start and day_start < 200 and val > day_start * 5:
                    continue
                rows.append({"ts": row.get("ts"), "equity": val})
    except Exception:  # noqa: BLE001
        return []
    return rows[-limit:]


def _positions(
    balance: dict[str, Any] | None, fills: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not balance:
        return []
    last_fill_by_ccy: dict[str, dict[str, Any]] = {}
    for d in fills:
        llm = d.get("llm") or {}
        gate = d.get("gate") or {}
        inst = str(llm.get("instId") or gate.get("instId") or "")
        ccy = inst.split("-")[0] if inst else ""
        if not ccy:
            continue
        last_fill_by_ccy[ccy] = {
            "action": llm.get("action"),
            "size": gate.get("size"),
            "entry_px": gate.get("entry_px"),
            "clOrdId": (d.get("order") or {}).get("clOrdId"),
            "ts": d.get("ts"),
        }

    out: list[dict[str, Any]] = []
    for d in balance.get("details") or []:
        ccy = str(d.get("ccy") or "")
        eq = _f(d.get("eq")) or 0.0
        if not ccy or eq <= 0:
            continue
        row = {
            "ccy": ccy,
            "eq": eq,
            "availBal": _f(d.get("availBal")),
            "ordFrozen": _f(d.get("ordFrozen")),
            "eqUsd": _f(d.get("eqUsd")),
            "is_quote": ccy == _QUOTE,
            "last_fill": last_fill_by_ccy.get(ccy),
        }
        out.append(row)
    out.sort(key=lambda r: (r["is_quote"], -(r.get("eqUsd") or 0), r["ccy"]))
    return out


def _story(decision: dict[str, Any] | None) -> dict[str, Any] | None:
    if not decision:
        return None
    llm = decision.get("llm") or {}
    gate = decision.get("gate") or {}
    order = decision.get("order") or {}
    return {
        "cycle": decision.get("cycle"),
        "ts": decision.get("ts"),
        "action": llm.get("action") or "HOLD",
        "instId": llm.get("instId") or gate.get("instId"),
        "source": llm.get("decision_source") or "—",
        "confidence": llm.get("confidence"),
        "llm_ok": llm.get("llm_ok"),
        "reason": llm.get("reason") or "",
        "gate_reason": gate.get("reason"),
        "gate_allowed": bool(gate.get("allowed")),
        "order_sent": bool(order.get("sent")),
        "order_ok": bool(order.get("ok")),
        "clOrdId": order.get("clOrdId"),
    }


def _scenario(decision: dict[str, Any] | None) -> dict[str, str]:
    """Jury-facing one-word scenario badge for the last cycle."""
    if not decision:
        return {
            "id": "IDLE",
            "label": "IDLE",
            "tone": "muted",
            "blurb": "Henüz cycle yok",
        }
    llm = decision.get("llm") or {}
    gate = decision.get("gate") or {}
    order = decision.get("order") or {}
    source = str(llm.get("decision_source") or "")
    action = str(llm.get("action") or "HOLD")
    gate_reason = str(gate.get("reason") or "")

    if order.get("sent") and order.get("ok"):
        return {
            "id": "FILL",
            "label": "FILL",
            "tone": "up",
            "blurb": f"{action} filled · {order.get('clOrdId') or 'ok'}",
        }
    if "fallback" in source.lower() or source == "rules_fallback":
        return {
            "id": "FALLBACK",
            "label": "FALLBACK",
            "tone": "warn",
            "blurb": "LLM fail → rules / HOLD (fail-closed)",
        }
    if action in ("BUY", "SELL") and not gate.get("allowed") and gate_reason not in (
        "SKIPPED",
        "",
        "—",
    ):
        return {
            "id": "GATED",
            "label": "GATED",
            "tone": "down",
            "blurb": f"{action} blocked · {gate_reason}",
        }
    if action == "HOLD" or gate_reason == "SKIPPED":
        return {
            "id": "HOLD",
            "label": "HOLD",
            "tone": "muted",
            "blurb": "No setup / no send",
        }
    return {
        "id": "DECIDE",
        "label": action,
        "tone": "muted",
        "blurb": f"{source or '—'} · {gate_reason or '—'}",
    }


def _fail_closed(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Evidence card: LLM failures never force a blind order."""
    fallbacks: list[dict[str, Any]] = []
    llm_fail = 0
    for d in decisions:
        llm = d.get("llm") or {}
        src = str(llm.get("decision_source") or "")
        if "fallback" in src.lower() or src == "rules_fallback":
            fallbacks.append(
                {
                    "cycle": d.get("cycle"),
                    "ts": d.get("ts"),
                    "action": llm.get("action"),
                    "reason": (llm.get("reason") or "")[:120],
                    "source": src,
                }
            )
        if llm.get("llm_ok") is False:
            llm_fail += 1
    last = fallbacks[-1] if fallbacks else None
    return {
        "fallback_count": len(fallbacks),
        "llm_fail_count": llm_fail,
        "last": last,
        "pitch": "LLM auth/schema/timeout → rules_fallback veya HOLD; asla 'yine de bas' yok.",
    }


def _risk_limits(state: dict[str, Any], equity: float | None) -> dict[str, Any]:
    day_start = _f(state.get("day_start_equity"))
    loss_pct = None
    if day_start and day_start > 0 and equity is not None:
        loss_pct = (equity - day_start) / day_start * 100.0
    orders_hour = len(state.get("order_timestamps") or [])
    return {
        "max_risk_per_trade_pct": float(config.MAX_RISK_PER_TRADE) * 100,
        "max_position_pct": float(config.MAX_POSITION_PCT) * 100,
        "max_exposure_pct": float(config.MAX_TOTAL_EXPOSURE) * 100,
        "daily_loss_limit_pct": float(config.DAILY_LOSS_LIMIT) * 100,
        "max_orders_per_hour": int(config.MAX_ORDERS_PER_HOUR),
        "max_spread_bps": float(config.MAX_SPREAD_BPS),
        "cutoff_new_buys": config.CUTOFF_NEW_BUYS,
        "hard_stop": config.HARD_STOP,
        "orders_this_hour": orders_hour,
        "day_pnl_pct": loss_pct,
        "kill_switch": bool(state.get("kill_switch")),
    }


def _okx_balance() -> dict[str, Any] | None:
    try:
        proc = subprocess.run(
            [_okx_bin(), "--json", "account", "balance"],
            capture_output=True,
            text=True,
            timeout=12,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout)
        if isinstance(data, list) and data:
            row = data[0]
        elif isinstance(data, dict):
            row = data
        else:
            return None
        details = []
        for d in row.get("details") or []:
            if not isinstance(d, dict):
                continue
            details.append(
                {
                    "ccy": d.get("ccy"),
                    "eq": d.get("eq"),
                    "availBal": d.get("availBal"),
                    "ordFrozen": d.get("ordFrozen"),
                    "eqUsd": d.get("eqUsd"),
                }
            )
        return {"totalEq": row.get("totalEq"), "details": details}
    except Exception:  # noqa: BLE001
        return None


def _pipeline(decision: dict[str, Any] | None) -> list[dict[str, str]]:
    """Map last decision into observe→decide→gate→order stages."""
    if not decision:
        return [
            {"id": "observe", "label": "OBSERVE", "status": "idle", "detail": "—"},
            {"id": "decide", "label": "DECIDE", "status": "idle", "detail": "—"},
            {"id": "gate", "label": "GATE", "status": "idle", "detail": "—"},
            {"id": "order", "label": "ORDER", "status": "idle", "detail": "—"},
        ]

    sigs = decision.get("signals_summary") or {}
    pairs = ", ".join(
        f"{k.split('-')[0]} {(v or {}).get('trend', '?')}" for k, v in sigs.items()
    ) or "markets"
    llm = decision.get("llm") or {}
    gate = decision.get("gate") or {}
    order = decision.get("order") or {}

    action = str(llm.get("action") or "HOLD")
    source = str(llm.get("decision_source") or "—")
    reason = str(llm.get("reason") or "")
    gate_reason = str(gate.get("reason") or "—")
    allowed = bool(gate.get("allowed"))

    if order.get("sent") and order.get("ok"):
        order_status, order_detail = "pass", str(order.get("clOrdId") or order.get("ordId") or "filled")
    elif order.get("sent"):
        order_status, order_detail = "fail", "send failed"
    elif action in ("BUY", "SELL") and not allowed:
        order_status, order_detail = "halt", gate_reason
    else:
        order_status, order_detail = "idle", "no send"

    if action in ("BUY", "SELL"):
        decide_status = "pass"
    else:
        decide_status = "hold"

    if allowed:
        gate_status = "pass"
    elif gate_reason in ("SKIPPED", "—", ""):
        gate_status = "idle"
    else:
        gate_status = "halt"

    return [
        {"id": "observe", "label": "OBSERVE", "status": "pass", "detail": pairs},
        {
            "id": "decide",
            "label": "DECIDE",
            "status": decide_status,
            "detail": f"{action} · {source}" + (f" · {reason}" if reason else ""),
        },
        {"id": "gate", "label": "GATE", "status": gate_status, "detail": gate_reason},
        {"id": "order", "label": "ORDER", "status": order_status, "detail": order_detail},
    ]


def build_snapshot() -> dict[str, Any]:
    state = _read_json(LOGS / "state.json")
    decisions = _tail_jsonl(LOGS / "decisions.jsonl", 80)
    all_for_fills = _tail_jsonl(LOGS / "decisions.jsonl", 500)
    last = decisions[-1] if decisions else None
    live_path = LOGS / "agent_live.log"
    live_lines = _tail_text(live_path, 12)
    live_age = None
    if live_path.exists():
        live_age = time.time() - live_path.stat().st_mtime

    cycle_from_log = None
    for line in reversed(live_lines):
        m = _CYCLE_RE.search(line)
        if m:
            cycle_from_log = int(m.group(1))
            break

    # Prefer journal equity that includes coins when available; overlay OKX totalEq
    balance = _okx_balance()
    equity_journal = float((last or {}).get("equity") or 0)
    equity_live = None
    if balance and balance.get("totalEq") not in (None, ""):
        try:
            equity_live = float(balance["totalEq"])
        except (TypeError, ValueError):
            equity_live = None

    day_start = state.get("day_start_equity")
    try:
        day_start_f = float(day_start) if day_start is not None else None
    except (TypeError, ValueError):
        day_start_f = None

    eq = equity_live if equity_live is not None else equity_journal
    pnl_pct = None
    if day_start_f and day_start_f > 0 and eq is not None:
        pnl_pct = (eq - day_start_f) / day_start_f * 100.0

    agent_alive = live_age is not None and live_age < 120

    fills = [
        d
        for d in all_for_fills
        if (d.get("order") or {}).get("ok") and (d.get("order") or {}).get("sent")
    ]

    gate_stats = _gate_stats(all_for_fills)
    equity_series = _equity_series(day_start_f)
    positions = _positions(balance, fills)
    story = _story(last)
    scenario = _scenario(last)
    fail_closed = _fail_closed(all_for_fills)
    risk = _risk_limits(state, eq)

    latest_fill = None
    if fills:
        f = fills[-1]
        llm = f.get("llm") or {}
        gate = f.get("gate") or {}
        order = f.get("order") or {}
        latest_fill = {
            "cycle": f.get("cycle"),
            "ts": f.get("ts"),
            "action": llm.get("action"),
            "instId": llm.get("instId") or gate.get("instId"),
            "size": gate.get("size"),
            "entry_px": gate.get("entry_px"),
            "clOrdId": order.get("clOrdId"),
        }

    return {
        "ts": time.time(),
        "brand": "XLogic",
        "tagline": "Fail-closed spot agent — every order through the gate",
        "agent_alive": agent_alive,
        "live_age_sec": live_age,
        "state": {
            "cycle": state.get("cycle") or cycle_from_log,
            "kill_switch": bool(state.get("kill_switch")),
            "hold_only": bool(state.get("hold_only")),
            "day_start_equity": day_start_f,
            "orders_hour": len(state.get("order_timestamps") or []),
            "pending": len(state.get("pending_orders") or {}),
        },
        "equity": eq,
        "equity_journal": equity_journal,
        "pnl_pct": pnl_pct,
        "balance": balance,
        "positions": positions,
        "story": story,
        "scenario": scenario,
        "fail_closed": fail_closed,
        "gate_stats": gate_stats,
        "risk": risk,
        "equity_series": equity_series,
        "latest_fill": latest_fill,
        "last": last,
        "pipeline": _pipeline(last),
        "recent": list(reversed(decisions[-24:])),
        "fills": list(reversed(fills[-8:])),
        "live_lines": live_lines,
        "mode": (last or {}).get("mode") or ("LIVE" if agent_alive else "—"),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:  # quieter
        if "/api/" in (args[0] if args else ""):
            return
        super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            html = (STATIC / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path == "/api/snapshot":
            payload = json.dumps(build_snapshot(), ensure_ascii=False, default=str).encode("utf-8")
            self._send(200, payload, "application/json; charset=utf-8")
            return
        if path.startswith("/static/"):
            rel = path[len("/static/") :]
            file = STATIC / rel
            if file.is_file() and STATIC in file.resolve().parents:
                ctype = "text/css" if file.suffix == ".css" else "application/octet-stream"
                self._send(200, file.read_bytes(), ctype)
                return
        self._send(404, b"not found", "text/plain")


def main() -> None:
    STATIC.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"XLogic console → http://{HOST}:{PORT}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)


if __name__ == "__main__":
    main()
