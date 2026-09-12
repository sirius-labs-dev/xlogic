#!/usr/bin/env python3
"""Live sanity check: print signals for BTC-USDT and ETH-USDT."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.signals import get_all_signals

PAIRS = ["BTC-USDT", "ETH-USDT"]
COLS = [
    "instId",
    "last",
    "bid",
    "ask",
    "spread_bps",
    "ema20",
    "ema50",
    "atr14",
    "trend",
    "entry_candidate",
    "suggested_stop",
    "suggested_target",
    "exit_signal",
    "chg24h_pct",
]


def main() -> int:
    signals = get_all_signals(PAIRS)
    print(f"{'instId':<10} {'last':>10} {'spread_bps':>10} {'ema20':>10} {'ema50':>10} "
          f"{'atr14':>10} {'atr%':>7} {'trend':>6} {'entry':>6} {'exit':>5} {'chg24h%':>8}")
    print("-" * 110)
    for inst in PAIRS:
        s = signals[inst]
        if not s.get("ok", True) or "error" in s and "last" not in s:
            print(f"{inst:<10} ERROR: {s.get('error')}")
            continue
        atr_pct = (s["atr14"] / s["last"] * 100.0) if s["last"] else 0.0
        print(
            f"{s['instId']:<10} {s['last']:>10.2f} {s['spread_bps']:>10.2f} "
            f"{s['ema20']:>10.2f} {s['ema50']:>10.2f} {s['atr14']:>10.2f} "
            f"{atr_pct:>6.2f}% {s['trend']:>6} {str(s['entry_candidate']):>6} "
            f"{str(s['exit_signal']):>5} {s['chg24h_pct']:>8.2f}"
        )
        # Sanity notes
        ema_near = abs(s["ema20"] - s["last"]) / s["last"] * 100
        print(
            f"  stop={s['suggested_stop']} target={s['suggested_target']} "
            f"| |ema20-last|={ema_near:.2f}% of price"
        )
        if inst == "BTC-USDT" and s["spread_bps"] >= 1.0:
            print("  WARNING: BTC spread >= 1 bps — check spread formula")
        if not (0.05 <= atr_pct <= 3.0):
            print(f"  NOTE: ATR%={atr_pct:.3f} outside typical 0.1–2% band (15m ATR)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
