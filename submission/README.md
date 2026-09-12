# XLogic — submission

**Hackathon:** Komünite × OKX TR Agentic Trading Hackathon (12 Eylül 2026)  
**Ürün:** Fail-closed otonom spot trading ajanı  
**Tek cümle:** Her emri risk kapısından geçirmeden borsaya yollamayan, stop’u borsada ilişik tutan ajan — LLM susunca da rules ile otonom kalır.

## Demo

| | |
|--|--|
| Canlı konsol | `bash scripts/run_dashboard.sh` · SS: [`xlogic-console.png`](./xlogic-console.png) |
| Agent | `python -m agent.main --decision auto` (Anthropic) veya `--decision rules` |
| Log | `logs/agent_live.log`, `logs/decisions.jsonl` |
| Performans | [`performance.md`](./performance.md) · [`performance.json`](./performance.json) |
| Sunum | [`xlogic-pitch.html`](./xlogic-pitch.html) — ←→ / space · N notes · F fullscreen |
| GIF notu | [`DEMO.md`](./DEMO.md) → `xlogic-demo.gif` |

Yenile: `python -m tools.metrics`

## Mimari

```
OBSERVE (okx CLI --json)
  → SIGNALS (EMA20/50, ATR14)
  → DECIDE (Anthropic LLM | rules fail-closed)
  → GATE (risk_gate — boyut burada)
  → ORDER (spot place + ilişik SL/TP)
  → JOURNAL (decisions.jsonl / equity.csv)
```

ATK entegrasyonu: tüm OKX çağrıları `/opt/homebrew/bin/okx --json` subprocess; imza yok. Tool listesi: `docs/okx-tools.json`.

## Fail-closed autonomy

- LLM 401 / timeout → `rules_fallback` veya HOLD (asla “yine de bas”).
- Setup yok → HOLD.
- Gate red (`BELOW_MIN_SIZE`, `RATE_LIMIT`, `DAILY_LOSS_KILL`, …) → emir gitmez (**GATED**).
- Alımda stop zorunlu; `--slTriggerPx` / `--tpTriggerPx` emre ilişik (`--slOrdPx=-1` market).
- Kill-switch disk’te kalıcı (`logs/state.json`).
- Cutoff 19:15 yeni alım yok; hard stop 19:20.

## Canlı kanıt (gün içi)

- **Fill #1:** BUY ETH-USDT ~0.00355 @ ~2534 · `clOrdId=agt1789207399030o7tk`
- **Fill #2 (exit):** SELL ETH-USDT ~0.003546 @ ~2544.6 · `agt1789221591956oth1` (OCO iptal → risk_gate → spot)
- **Fill #3 (re-entry):** BUY ETH-USDT ~0.003537 @ ~2544 · `agt1789221622666pc19` + ilişik SL/TP
- **Kasa:** day start 30 USDT → ~30.00 totalEq
- **LLM:** `--decision auto` + journal’da `llm_ok` / `rules_fallback`
- **Gate örneği:** BUY + dolu pozisyon → `OVER_POSITION` / `BELOW_MIN_SIZE` (fren)

## Risk limitleri (`config.py`)

| Parametre | Değer |
|-----------|--------|
| Pairs | BTC-USDT, ETH-USDT |
| Risk / trade | %0.5 |
| Max position | %30 |
| Max exposure | %60 |
| Daily loss kill | %2 |
| Max orders / hour | 6 |
| Max spread | 10 bps |
| Cutoff / hard stop | 19:15 / 19:20 |

## Dosyalar

| Yol | Rol |
|-----|-----|
| `agent/main.py` | 60 sn döngü |
| `tools/okx_client.py` | CLI sarmalayıcı |
| `tools/risk_gate.py` | Emir kapısı + testler |
| `tools/signals.py` | Sinyal motoru |
| `tools/llm.py` | Anthropic / OpenAI-compatible |
| `tools/rule_decision.py` | Rules fallback |
| `tools/journal.py` | Append-only günlük |
| `tools/metrics.py` | Performans dump |
| `dashboard/` | XLogic UX konsolu |

## Pitch (≤60 sn)

“XLogic, OKX TR spot’ta otonom long ajan. Sinyal üretir, LLM veya rules karar verir, ama **hiçbir emir risk kapısından geçmeden çıkmaz**. Stop borsada ilişik; LLM çökse bile fail-closed rules ile döngü sürer. Bugün canlı fill + XLogic konsolunda gate frenlerini gösteriyoruz.”

## Güvenlik notu

API anahtarları teslim klasöründe yok (`.env` / `~/.okx` gitignore). Demo sonrası key rotate önerilir.
