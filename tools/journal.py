"""Append-only decision and equity journals. Never raise; never log secrets."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
DECISIONS_PATH = LOG_DIR / "decisions.jsonl"
EQUITY_PATH = LOG_DIR / "equity.csv"

_SECRET_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "secret",
    "passphrase",
    "password",
    "token",
    "authorization",
    "openai_api_key",
    "okx_api_key",
    "okx_secret",
    "private_key",
}


def _warn(msg: str) -> None:
    print(f"[journal] {msg}", file=sys.stderr)


def _scrub(obj: Any) -> Any:
    """Drop credential-like keys recursively."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower().replace("-", "_")
            if lk in _SECRET_KEYS or "api_key" in lk or lk.endswith("_secret"):
                continue
            out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(x) for x in obj]
    return obj


def log_decision(record: dict) -> None:
    """Append one JSON line to logs/decisions.jsonl."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        payload = _scrub(dict(record))
        if "ts" not in payload:
            payload["ts"] = datetime.now(timezone.utc).isoformat()
        line = json.dumps(payload, ensure_ascii=False, default=str)
        with DECISIONS_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:  # noqa: BLE001
        _warn(f"log_decision failed: {exc}")


def log_equity(total_value: float, breakdown: dict) -> None:
    """Append one row to logs/equity.csv (ts, total_value, then ccy columns)."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat()
        clean = _scrub(breakdown) if isinstance(breakdown, dict) else {}
        # Stable column order: known keys first, then sorted extras
        ccy_keys = sorted(str(k) for k in clean.keys())
        write_header = not EQUITY_PATH.exists() or EQUITY_PATH.stat().st_size == 0
        # If file exists with different headers, still append compatible row
        fieldnames = ["ts", "total_value", *ccy_keys]
        if not write_header:
            try:
                with EQUITY_PATH.open("r", encoding="utf-8") as rf:
                    reader = csv.reader(rf)
                    existing = next(reader, None)
                if existing:
                    # keep existing header; fill missing with ""
                    fieldnames = list(existing)
            except Exception:
                pass

        row = {"ts": ts, "total_value": f"{float(total_value):.8f}"}
        for k in ccy_keys:
            row[k] = str(clean.get(k, ""))
        for fn in fieldnames:
            row.setdefault(fn, "")

        with EQUITY_PATH.open("a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if write_header:
                writer.writeheader()
            writer.writerow(row)
    except Exception as exc:  # noqa: BLE001
        _warn(f"log_equity failed: {exc}")
