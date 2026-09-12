"""OKX CLI wrapper — all API calls go through subprocess, never custom signing."""

from __future__ import annotations

import json
import subprocess
from typing import Any

OKX_BIN = "/opt/homebrew/bin/okx"
TIMEOUT_SEC = 15


def _clean_stderr(stderr: str) -> str:
    """Drop Node experimental warnings; keep real CLI error lines."""
    lines = []
    for line in (stderr or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if "UNDICI-EHPA" in s or "node --trace-warnings" in s:
            continue
        if s.startswith("(Use `node"):
            continue
        lines.append(s)
    return "\n".join(lines)


def _run(args: list[str]) -> dict[str, Any]:
    """Run `okx --json ...` and return {ok, data} or {ok, error}."""
    cmd = [OKX_BIN, "--json", *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"timeout after {TIMEOUT_SEC}s: {' '.join(cmd)}"}
    except FileNotFoundError:
        return {"ok": False, "error": f"okx binary not found: {OKX_BIN}"}
    except Exception as exc:  # noqa: BLE001 — never raise to callers
        return {"ok": False, "error": str(exc)}

    raw = (proc.stdout or "").strip()
    err = _clean_stderr(proc.stderr or "")

    if not raw:
        msg = err or f"empty stdout (exit {proc.returncode}): {' '.join(cmd)}"
        return {"ok": False, "error": msg}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": f"invalid JSON (exit {proc.returncode}): {raw[:500]}",
        }

    # CLI may put errors in JSON with non-zero exit, or code != "0"
    if proc.returncode != 0:
        if isinstance(data, dict):
            msg = (
                data.get("error")
                or data.get("msg")
                or data.get("message")
                or err
                or json.dumps(data)[:500]
            )
        else:
            msg = err or str(data)[:500]
        return {"ok": False, "error": str(msg)}

    if isinstance(data, dict):
        code = data.get("code")
        if code is not None and str(code) not in ("0", "null", ""):
            msg = data.get("msg") or data.get("error") or data.get("message") or json.dumps(data)[:500]
            return {"ok": False, "error": str(msg)}

    return {"ok": True, "data": data}


def get_balance() -> dict[str, Any]:
    return _run(["account", "balance"])


def get_ticker(instId: str) -> dict[str, Any]:
    return _run(["market", "ticker", instId])


def get_candles(instId: str, bar: str, limit: int) -> dict[str, Any]:
    return _run(
        ["market", "candles", instId, "--bar", str(bar), "--limit", str(limit)]
    )


def get_orderbook(instId: str, sz: int) -> dict[str, Any]:
    return _run(["market", "orderbook", instId, "--sz", str(sz)])


def get_instrument(instId: str) -> dict[str, Any]:
    return _run(
        ["market", "instruments", "--instType", "SPOT", "--instId", instId]
    )


def get_fees() -> dict[str, Any]:
    return _run(["account", "fees", "--instType", "SPOT"])


def get_open_orders(instId: str | None = None) -> dict[str, Any]:
    args = ["spot", "orders"]
    if instId:
        args.extend(["--instId", instId])
    return _run(args)


def get_fills(instId: str | None = None) -> dict[str, Any]:
    args = ["spot", "fills"]
    if instId:
        args.extend(["--instId", instId])
    return _run(args)


def cancel_order(
    instId: str,
    ordId: str | None = None,
    clOrdId: str | None = None,
) -> dict[str, Any]:
    if not ordId and not clOrdId:
        return {"ok": False, "error": "cancel_order requires ordId or clOrdId"}
    args = ["spot", "cancel", instId]
    if ordId:
        args.extend(["--ordId", ordId])
    else:
        args.extend(["--clOrdId", clOrdId])  # type: ignore[arg-type]
    return _run(args)


def cancel_all(instId: str) -> dict[str, Any]:
    """List open orders for instId and cancel each one."""
    listed = get_open_orders(instId)
    if not listed.get("ok"):
        return listed

    data = listed.get("data")
    orders: list[Any] = []
    if isinstance(data, list):
        orders = data
    elif isinstance(data, dict):
        # common shapes: {data: [...]} or CLI-wrapped list
        inner = data.get("data", data.get("orders", []))
        if isinstance(inner, list):
            orders = inner
        elif isinstance(data.get("data"), dict) and isinstance(
            data["data"].get("data"), list
        ):
            orders = data["data"]["data"]

    results: list[dict[str, Any]] = []
    for order in orders:
        if not isinstance(order, dict):
            continue
        oid = order.get("ordId") or order.get("orderId")
        cid = order.get("clOrdId")
        order_inst = order.get("instId") or instId
        if oid:
            results.append(cancel_order(order_inst, ordId=str(oid)))
        elif cid:
            results.append(cancel_order(order_inst, clOrdId=str(cid)))

    failed = [r for r in results if not r.get("ok")]
    if failed:
        return {
            "ok": False,
            "error": f"{len(failed)}/{len(results)} cancels failed",
            "data": results,
        }
    return {"ok": True, "data": results}


def place_limit_buy_with_stop(
    instId: str,
    sz: str,
    px: str,
    sl_trigger_px: str,
    tp_trigger_px: str,
    clOrdId: str,
) -> dict[str, Any]:
    return _run(
        [
            "spot",
            "place",
            "--instId",
            instId,
            "--side",
            "buy",
            "--ordType",
            "limit",
            "--sz",
            str(sz),
            "--px",
            str(px),
            "--tdMode",
            "cash",
            "--tgtCcy",
            "base_ccy",
            "--clOrdId",
            clOrdId,
            f"--slTriggerPx={sl_trigger_px}",
            "--slOrdPx=-1",
            f"--tpTriggerPx={tp_trigger_px}",
            "--tpOrdPx=-1",
        ]
    )


def place_limit_sell(
    instId: str,
    sz: str,
    px: str,
    clOrdId: str,
) -> dict[str, Any]:
    return _run(
        [
            "spot",
            "place",
            "--instId",
            instId,
            "--side",
            "sell",
            "--ordType",
            "limit",
            "--sz",
            str(sz),
            "--px",
            str(px),
            "--tdMode",
            "cash",
            "--tgtCcy",
            "base_ccy",
            "--clOrdId",
            clOrdId,
        ]
    )
