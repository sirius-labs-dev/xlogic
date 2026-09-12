# Performance dump — XLogic

Generated: `2026-09-12T18:50:39+03:00`

## Equity

| | |
|--|--|
| Day start | 30.0 |
| Last equity | 29.95055891 |
| PnL abs | -0.04944108999999841 |
| PnL % | -0.1648 |
| Kill switch | False |
| Cycles | 287 (state cycle 286) |
| Window | 2026-09-12T05:24:37+03:00 → 2026-09-12T18:49:53+03:00 |

## Decision mix

- Modes: `{'DRY': 11, 'LIVE': 276}`
- Actions: `{'HOLD': 160, 'BUY': 126, 'SELL': 1}`
- Sources: `{'?': 2, 'rules': 97, 'llm': 183, 'rules_fallback': 4, 'demo_exit': 1}`
- LLM ok count: **184**
- Gate reasons: `{'SKIPPED': 160, 'OK': 11, 'RATE_LIMIT': 3, 'DAILY_LOSS_KILL': 1, 'BELOW_MIN_SIZE': 78, 'OVER_POSITION': 34}`
- GATED (non-OK/SKIPPED gates): **116**

## Fills (4)

- `2026-09-12T13:03:10+03:00` **BUY** ETH-USDT sz=0.00355 @ 2534.87 · `agt1789207399030o7tk`
- `2026-09-12T16:59:52+03:00` **SELL** ETH-USDT sz=0.003546 @ 2544.38 · `agt1789221591956oth1`
- `2026-09-12T17:00:04+03:00` **BUY** ETH-USDT sz=0.003537 @ 2544.84 · `agt1789221622666pc19`
- `2026-09-12T18:37:33+03:00` **BUY** ETH-USDT sz=0.003546 @ 2534.18 · `agt1789227465820421u`

## Autonomy proof

- Spot orders use `clOrdId` prefix `agt…` (CLI has `--aiBuilderCode`, no `--tag`).
- Every candidate order passes `tools/risk_gate.py` before `okx spot place`.
- Fail-closed: LLM auth failure → `rules_fallback` / HOLD; gate reject → no send.
