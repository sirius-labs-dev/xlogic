"""Market signals — EMA/ATR/trend for allowed pairs via okx_client."""

from __future__ import annotations

from typing import Any

from tools import okx_client

EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14
MIN_1H_CLOSED = 60
MIN_15M_CLOSED = 30
CANDLE_LIMIT_1H = 100
CANDLE_LIMIT_15M = 100
ENTRY_SPREAD_BPS = 10.0


def _f(value: Any) -> float:
    return float(value)


def _round_px(value: float) -> float:
    return round(value, 2)


def _round2(value: float) -> float:
    return round(value, 2)


def prepare_candles(raw: list[Any]) -> list[dict[str, float]]:
    """
    Drop unconfirmed (confirm != \"1\"), convert fields, oldest -> newest.

    Candle row: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
    """
    closed: list[list[Any]] = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 9:
            continue
        if str(row[8]) != "1":
            continue
        closed.append(list(row))

    # API is newest-first; reverse to oldest-first for indicators
    closed.reverse()

    out: list[dict[str, float]] = []
    for row in closed:
        out.append(
            {
                "ts": _f(row[0]),
                "open": _f(row[1]),
                "high": _f(row[2]),
                "low": _f(row[3]),
                "close": _f(row[4]),
                "vol": _f(row[5]),
            }
        )
    return out


def ema(closes: list[float], period: int) -> list[float | None]:
    """
    EMA with SMA seed on first `period` values, then k = 2/(period+1).

    Returns a list aligned with closes; leading values before the seed are None.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < period:
        return out

    k = 2.0 / (period + 1)
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, n):
        prev = closes[i] * k + prev * (1.0 - k)
        out[i] = prev
    return out


def true_ranges(candles: list[dict[str, float]]) -> list[float]:
    """Wilder True Range series (one fewer than candles; starts at index 1)."""
    trs: list[float] = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        prev_c = candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
    return trs


def atr_wilder(candles: list[dict[str, float]], period: int = ATR_PERIOD) -> float | None:
    """Wilder ATR: first ATR = SMA of first `period` TRs, then smoothed."""
    trs = true_ranges(candles)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _extract_rows(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        inner = payload.get("data", payload)
        if isinstance(inner, list):
            return inner
    return []


def _ticker_fields(ticker_data: Any) -> dict[str, float] | None:
    rows = _extract_rows(ticker_data)
    if not rows or not isinstance(rows[0], dict):
        return None
    row = rows[0]
    try:
        last = _f(row["last"])
        bid = _f(row.get("bidPx", row.get("bid")))
        ask = _f(row.get("askPx", row.get("ask")))
        open24h = _f(row["open24h"])
    except (KeyError, TypeError, ValueError):
        return None
    if open24h == 0:
        return None
    chg24h_pct = (last - open24h) / open24h * 100.0
    mid = (ask + bid) / 2.0
    if mid <= 0:
        return None
    spread_bps = (ask - bid) / mid * 10000.0
    return {
        "last": last,
        "bid": bid,
        "ask": ask,
        "chg24h_pct": chg24h_pct,
        "spread_bps": spread_bps,
    }


def compute_signal(inst_id: str) -> dict[str, Any]:
    """Build the LLM-facing signal dict for one instrument."""
    candles_1h_res = okx_client.get_candles(inst_id, "1H", CANDLE_LIMIT_1H)
    if not candles_1h_res.get("ok"):
        return {"ok": False, "error": candles_1h_res.get("error", "candles_1h_failed")}

    candles_15m_res = okx_client.get_candles(inst_id, "15m", CANDLE_LIMIT_15M)
    if not candles_15m_res.get("ok"):
        return {"ok": False, "error": candles_15m_res.get("error", "candles_15m_failed")}

    ticker_res = okx_client.get_ticker(inst_id)
    if not ticker_res.get("ok"):
        return {"ok": False, "error": ticker_res.get("error", "ticker_failed")}

    c1h = prepare_candles(_extract_rows(candles_1h_res["data"]))
    c15 = prepare_candles(_extract_rows(candles_15m_res["data"]))

    if len(c1h) < MIN_1H_CLOSED:
        return {"ok": False, "error": "INSUFFICIENT_CANDLES"}
    if len(c15) < MIN_15M_CLOSED:
        return {"ok": False, "error": "INSUFFICIENT_CANDLES"}

    closes_1h = [c["close"] for c in c1h]
    ema20_series = ema(closes_1h, EMA_FAST)
    ema50_series = ema(closes_1h, EMA_SLOW)
    ema20 = ema20_series[-1]
    ema50 = ema50_series[-1]
    if ema20 is None or ema50 is None:
        return {"ok": False, "error": "INSUFFICIENT_CANDLES"}

    atr14 = atr_wilder(c15, ATR_PERIOD)
    if atr14 is None:
        return {"ok": False, "error": "INSUFFICIENT_CANDLES"}

    tick = _ticker_fields(ticker_res["data"])
    if tick is None:
        return {"ok": False, "error": "BAD_TICKER"}

    last = tick["last"]
    spread_bps = tick["spread_bps"]
    trend = "UP" if (last > ema50 and ema20 > ema50) else "DOWN"
    entry_candidate = trend == "UP" and spread_bps <= ENTRY_SPREAD_BPS

    # Last CLOSED 1H close vs EMA50 (c1h is already closed-only)
    last_closed_1h = c1h[-1]["close"]
    exit_signal = last_closed_1h < ema50

    return {
        "instId": inst_id,
        "last": _round_px(last),
        "bid": _round_px(tick["bid"]),
        "ask": _round_px(tick["ask"]),
        "spread_bps": _round2(spread_bps),
        "ema20": _round_px(ema20),
        "ema50": _round_px(ema50),
        "atr14": _round_px(atr14),
        "trend": trend,
        "entry_candidate": bool(entry_candidate),
        "suggested_stop": _round_px(last - 2.0 * atr14),
        "suggested_target": _round_px(last + 3.0 * atr14),
        "exit_signal": bool(exit_signal),
        "chg24h_pct": _round2(tick["chg24h_pct"]),
        "ok": True,
    }


def get_all_signals(pairs: list[str]) -> dict[str, dict[str, Any]]:
    """Compute signals for each pair; failures are isolated per instrument."""
    out: dict[str, dict[str, Any]] = {}
    for inst_id in pairs:
        try:
            out[inst_id] = compute_signal(inst_id)
        except Exception as exc:  # noqa: BLE001 — never break the batch
            out[inst_id] = {"ok": False, "error": str(exc)}
    return out
