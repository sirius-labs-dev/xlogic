"""Performance metrics from journals — no secrets, no network required."""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
DECISIONS_PATH = LOG_DIR / "decisions.jsonl"
EQUITY_PATH = LOG_DIR / "equity.csv"
STATE_PATH = LOG_DIR / "state.json"


def _load_decisions(path: Path = DECISIONS_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _load_equity_series(path: Path = EQUITY_PATH) -> list[dict[str, Any]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                rows.append(
                    {
                        "ts": row.get("ts"),
                        "total_value": float(row.get("total_value") or 0),
                    }
                )
            except (TypeError, ValueError):
                continue
    return rows


def compute(decisions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Aggregate hackathon-facing metrics from decisions.jsonl + state."""
    decisions = decisions if decisions is not None else _load_decisions()
    state = _load_state()
    equity_rows = _load_equity_series()

    modes = Counter(str(d.get("mode") or "?") for d in decisions)
    actions = Counter(str((d.get("llm") or {}).get("action") or "?") for d in decisions)
    sources = Counter(
        str((d.get("llm") or {}).get("decision_source") or "?") for d in decisions
    )
    gates = Counter(str((d.get("gate") or {}).get("reason") or "?") for d in decisions)

    fills = [
        d
        for d in decisions
        if (d.get("order") or {}).get("sent") and (d.get("order") or {}).get("ok")
    ]
    braked = [
        d
        for d in decisions
        if (d.get("gate") or {}).get("reason")
        not in (None, "OK", "SKIPPED", "?")
    ]
    llm_ok_n = sum(1 for d in decisions if (d.get("llm") or {}).get("llm_ok") is True)

    day_start = state.get("day_start_equity")
    try:
        day_start_f = float(day_start) if day_start is not None else None
    except (TypeError, ValueError):
        day_start_f = None

    last_eq = None
    if decisions:
        try:
            last_eq = float(decisions[-1].get("equity") or 0)
        except (TypeError, ValueError):
            last_eq = None
    if equity_rows:
        last_eq = equity_rows[-1]["total_value"]

    pnl_abs = None
    pnl_pct = None
    if day_start_f and day_start_f > 0 and last_eq is not None:
        pnl_abs = last_eq - day_start_f
        pnl_pct = pnl_abs / day_start_f * 100.0

    fill_summaries = []
    for d in fills:
        llm = d.get("llm") or {}
        gate = d.get("gate") or {}
        order = d.get("order") or {}
        fill_summaries.append(
            {
                "ts": d.get("ts"),
                "cycle": d.get("cycle"),
                "action": llm.get("action"),
                "instId": llm.get("instId") or gate.get("instId"),
                "size": gate.get("size"),
                "entry_px": gate.get("entry_px"),
                "stop_px": gate.get("stop_px"),
                "take_profit_px": gate.get("take_profit_px"),
                "clOrdId": order.get("clOrdId"),
                "ordId": order.get("ordId"),
                "decision_source": llm.get("decision_source"),
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "cycles": len(decisions),
        "state_cycle": state.get("cycle"),
        "kill_switch": bool(state.get("kill_switch")),
        "day_start_equity": day_start_f,
        "last_equity": last_eq,
        "pnl_abs": pnl_abs,
        "pnl_pct": pnl_pct,
        "modes": dict(modes),
        "actions": dict(actions),
        "decision_sources": dict(sources),
        "gate_reasons": dict(gates),
        "llm_ok_count": llm_ok_n,
        "fill_count": len(fills),
        "braked_count": len(braked),
        "fills": fill_summaries,
        "equity_points": len(equity_rows),
        "first_ts": decisions[0].get("ts") if decisions else None,
        "last_ts": decisions[-1].get("ts") if decisions else None,
    }


def write_dump(out_dir: Path | None = None) -> dict[str, Path]:
    """Write JSON + Markdown performance dump under submission/."""
    out_dir = out_dir or (ROOT / "submission")
    out_dir.mkdir(parents=True, exist_ok=True)
    m = compute()

    json_path = out_dir / "performance.json"
    md_path = out_dir / "performance.md"
    json_path.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    fills_md = "\n".join(
        f"- `{f.get('ts')}` **{f.get('action')}** {f.get('instId')} "
        f"sz={f.get('size')} @ {f.get('entry_px')} · `{f.get('clOrdId')}`"
        for f in m.get("fills") or []
    ) or "- (henüz fill yok)"

    md = f"""# Performance dump — XLogic

Generated: `{m['generated_at']}`

## Equity

| | |
|--|--|
| Day start | {m.get('day_start_equity')} |
| Last equity | {m.get('last_equity')} |
| PnL abs | {m.get('pnl_abs')} |
| PnL % | {None if m.get('pnl_pct') is None else round(m['pnl_pct'], 4)} |
| Kill switch | {m.get('kill_switch')} |
| Cycles | {m.get('cycles')} (state cycle {m.get('state_cycle')}) |
| Window | {m.get('first_ts')} → {m.get('last_ts')} |

## Decision mix

- Modes: `{m.get('modes')}`
- Actions: `{m.get('actions')}`
- Sources: `{m.get('decision_sources')}`
- LLM ok count: **{m.get('llm_ok_count')}**
- Gate reasons: `{m.get('gate_reasons')}`
- GATED (non-OK/SKIPPED gates): **{m.get('braked_count')}**

## Fills ({m.get('fill_count')})

{fills_md}

## Autonomy proof

- Spot orders use `clOrdId` prefix `agt…` (CLI has `--aiBuilderCode`, no `--tag`).
- Every candidate order passes `tools/risk_gate.py` before `okx spot place`.
- Fail-closed: LLM auth failure → `rules_fallback` / HOLD; gate reject → no send.
"""
    md_path.write_text(md, encoding="utf-8")
    return {"json": json_path, "md": md_path, "metrics": m}  # type: ignore[dict-item]


if __name__ == "__main__":
    paths = write_dump()
    print(json.dumps({k: str(v) if k != "metrics" else v for k, v in paths.items()}, ensure_ascii=False, indent=2, default=str))
