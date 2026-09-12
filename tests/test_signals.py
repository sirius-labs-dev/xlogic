"""Unit tests for tools.signals — mocked okx_client, no network."""

from __future__ import annotations

import pytest

from tools import signals


def _candle(ts, o, h, l, c, confirm="1"):
    return [
        str(ts),
        str(o),
        str(h),
        str(l),
        str(c),
        "1",
        "1",
        "1",
        str(confirm),
    ]


def test_prepare_candles_drops_unconfirmed_and_reverses():
    # newest-first from API
    raw = [
        _candle(300, 3, 3, 3, 3, confirm="0"),  # forming — drop
        _candle(200, 2, 2, 2, 2, confirm="1"),
        _candle(100, 1, 1, 1, 1, confirm="1"),
    ]
    got = signals.prepare_candles(raw)
    assert [c["ts"] for c in got] == [100.0, 200.0]
    assert [c["close"] for c in got] == [1.0, 2.0]


def test_ema_matches_hand_calculation():
    # period=3, closes = 1,2,3,4,5
    # seed SMA = (1+2+3)/3 = 2
    # k = 2/4 = 0.5
    # ema4 = 4*0.5 + 2*0.5 = 3
    # ema5 = 5*0.5 + 3*0.5 = 4
    closes = [1.0, 2.0, 3.0, 4.0, 5.0]
    series = signals.ema(closes, 3)
    assert series[0] is None
    assert series[1] is None
    assert series[2] == pytest.approx(2.0)
    assert series[3] == pytest.approx(3.0)
    assert series[4] == pytest.approx(4.0)


def test_atr_wilder_matches_hand_calculation():
    # Build 5 candles -> 4 TRs; period=3
    # c0: h=10,l=9,c=9.5
    # c1: h=11,l=9.5,c=10.5  TR=max(1.5, |11-9.5|, |9.5-9.5|)=1.5
    # c2: h=12,l=10,c=11     TR=max(2, |12-10.5|, |10-10.5|)=2
    # c3: h=11,l=10,c=10.5   TR=max(1, |11-11|, |10-11|)=1
    # c4: h=13,l=10.5,c=12.5 TR=max(2.5, |13-10.5|, |10.5-10.5|)=2.5
    candles = [
        {"high": 10.0, "low": 9.0, "close": 9.5},
        {"high": 11.0, "low": 9.5, "close": 10.5},
        {"high": 12.0, "low": 10.0, "close": 11.0},
        {"high": 11.0, "low": 10.0, "close": 10.5},
        {"high": 13.0, "low": 10.5, "close": 12.5},
    ]
    # first ATR = (1.5+2+1)/3 = 4.5/3 = 1.5
    # next = (1.5*2 + 2.5)/3 = 5.5/3 = 1.8333...
    atr = signals.atr_wilder(candles, period=3)
    assert atr == pytest.approx(5.5 / 3.0)


def test_insufficient_candles(monkeypatch):
    def fake_candles(inst_id, bar, limit):
        # newest-first, all confirmed, but only 10 rows
        rows = [_candle(i, 100 + i, 101 + i, 99 + i, 100 + i) for i in range(10, 0, -1)]
        return {"ok": True, "data": rows}

    def fake_ticker(inst_id):
        return {
            "ok": True,
            "data": [
                {
                    "last": "100",
                    "bidPx": "99.9",
                    "askPx": "100.1",
                    "open24h": "98",
                }
            ],
        }

    monkeypatch.setattr(signals.okx_client, "get_candles", fake_candles)
    monkeypatch.setattr(signals.okx_client, "get_ticker", fake_ticker)
    result = signals.compute_signal("BTC-USDT")
    assert result["ok"] is False
    assert result["error"] == "INSUFFICIENT_CANDLES"


def test_get_all_signals_isolates_errors(monkeypatch):
    def fake_candles(inst_id, bar, limit):
        if inst_id == "BAD-USDT":
            return {"ok": False, "error": "boom"}
        # enough closed candles
        n = 80
        rows = []
        for i in range(n, 0, -1):
            px = 100.0 + i * 0.1
            rows.append(_candle(i, px, px + 1, px - 1, px, confirm="1"))
        # prepend forming candle
        rows.insert(0, _candle(n + 1, 200, 201, 199, 200, confirm="0"))
        return {"ok": True, "data": rows}

    def fake_ticker(inst_id):
        return {
            "ok": True,
            "data": [
                {
                    "last": "108",
                    "bidPx": "107.99",
                    "askPx": "108.01",
                    "open24h": "100",
                }
            ],
        }

    monkeypatch.setattr(signals.okx_client, "get_candles", fake_candles)
    monkeypatch.setattr(signals.okx_client, "get_ticker", fake_ticker)

    out = signals.get_all_signals(["BTC-USDT", "BAD-USDT"])
    assert out["BAD-USDT"]["ok"] is False
    assert "boom" in out["BAD-USDT"]["error"]
    assert out["BTC-USDT"].get("ok") is True
    assert out["BTC-USDT"]["instId"] == "BTC-USDT"
    assert "ema20" in out["BTC-USDT"]
    assert "spread_bps" in out["BTC-USDT"]


def test_entry_and_exit_logic(monkeypatch):
    # Construct rising series so EMA20 > EMA50 and last > ema50
    def fake_candles(inst_id, bar, limit):
        n = 80
        # newest-first: index 0 = newest closed bar (highest price)
        rows = []
        for i in range(n, 0, -1):  # i=n newest, i=1 oldest
            px = 100.0 + i
            rows.append(_candle(i * 1000, px - 0.5, px + 0.5, px - 1, px))
        rows.insert(0, _candle(999999, 999, 1000, 998, 999, confirm="0"))
        return {"ok": True, "data": rows}

    def fake_ticker(inst_id):
        return {
            "ok": True,
            "data": [
                {
                    "last": "180",
                    "bidPx": "179.999",
                    "askPx": "180.001",
                    "open24h": "150",
                }
            ],
        }

    monkeypatch.setattr(signals.okx_client, "get_candles", fake_candles)
    monkeypatch.setattr(signals.okx_client, "get_ticker", fake_ticker)
    sig = signals.compute_signal("ETH-USDT")
    assert sig["ok"] is True
    assert sig["trend"] in ("UP", "DOWN")
    assert isinstance(sig["entry_candidate"], bool)
    assert isinstance(sig["exit_signal"], bool)
    # tight spread ~0.55 bps
    assert sig["spread_bps"] < 1.0
    assert sig["suggested_stop"] < sig["last"] < sig["suggested_target"]


def test_confirm_zero_excluded_from_ema_input(monkeypatch):
    """Forming candle must not affect closes used for EMA."""
    captured = {}

    real_ema = signals.ema

    def wrap_ema(closes, period):
        captured["closes"] = list(closes)
        return real_ema(closes, period)

    def fake_candles(inst_id, bar, limit):
        n = 80
        rows = [_candle(i, 10, 11, 9, 10 + i / 100.0) for i in range(n, 0, -1)]
        rows.insert(0, _candle(10_000, 50, 51, 49, 999.0, confirm="0"))
        return {"ok": True, "data": rows}

    def fake_ticker(inst_id):
        return {
            "ok": True,
            "data": [{"last": "12", "bidPx": "11.99", "askPx": "12.01", "open24h": "11"}],
        }

    monkeypatch.setattr(signals, "ema", wrap_ema)
    monkeypatch.setattr(signals.okx_client, "get_candles", fake_candles)
    monkeypatch.setattr(signals.okx_client, "get_ticker", fake_ticker)
    signals.compute_signal("BTC-USDT")
    assert 999.0 not in captured["closes"]
    assert len(captured["closes"]) == 80
