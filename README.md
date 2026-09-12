# XLogic

**Komünite × OKX TR Agentic Trading Hackathon — 12 Eylül 2026**

Fail-closed otonom spot ajanı: her emir **risk kapısından** geçmeden borsaya gitmez; stop borsada ilişik; LLM susunca rules ile döngü sürer.

> Teslim: [`submission/`](./submission/) · Pitch: [`submission/xlogic-pitch.html`](./submission/xlogic-pitch.html) · Metrik: [`submission/performance.md`](./submission/performance.md) · Konsol SS: [`submission/xlogic-console.png`](./submission/xlogic-console.png)

## Jüri özeti (30 sn)

| Kriter | Kanıt |
|--------|--------|
| **Functional** | Canlı OKX TR spot loop · 4 fill · `clOrdId` prefix `agt…` |
| **UX** | XLogic live console (`dashboard/`) · pitch deck |
| **ATK / CLI** | Tüm OKX çağrıları `okx --json` subprocess · imza yok · `docs/okx-tools.json` |
| **Reliability** | Fail-closed: LLM fail → `rules_fallback` / HOLD · gate red → **GATED** (emir yok) · 40 pytest |
| **Risk** | %0.5/trade · %30 position · %60 exposure · %2 daily kill · 6/saat · cutoff 19:15 |

## Canlı kanıt

- **BUY #1** ETH-USDT ~0.00355 @ ~2534 · `agt1789207399030o7tk`
- **SELL** ~0.003546 @ ~2544.6 · `agt1789221591956oth1`
- **BUY #2** + ilişik SL/TP · `agt1789221622666pc19`
- **BUY #3** ~0.003546 @ ~2534 · `agt1789227465820421u` (LLM)
- Day start **$30** → ~$29.95 · gate frenleri: `BELOW_MIN_SIZE`, `OVER_POSITION`, `RATE_LIMIT`
- ~287 cycle · Karar: Anthropic LLM (`--decision auto`) + `rules_fallback`

## Mimari

```
OBSERVE (okx CLI --json)
  → SIGNALS (EMA20/50, ATR14)
  → DECIDE (LLM | rules fail-closed)
  → GATE (risk_gate boyutlar / reddeder)
  → ORDER (spot limit + SL/TP ilişik)
  → JOURNAL (decisions.jsonl / equity.csv)
```

**ATK derinliği:** Market / account / spot tool’ları CLI üzerinden decision loop’ta. Özel HMAC yok — CLI kimlik + `--json`. Cursor’da OKX Trade MCP (`market,account,spot`) aynı profil ile kullanılabilir.

| Modül | Rol |
|-------|-----|
| `agent/main.py` | ~60s döngü |
| `tools/okx_client.py` | CLI sarmalayıcı |
| `tools/signals.py` | Trend / entry / exit |
| `tools/llm.py` | Anthropic (öncelik) |
| `tools/rule_decision.py` | Fail-closed rules |
| `tools/risk_gate.py` | 13 red kodu + kill-switch |
| `tools/journal.py` / `metrics.py` | Günlük + dump |
| `dashboard/` | Live console |
| `config.py` | Limitler / çiftler / cutoff |

## Fail-closed

- LLM 401 / timeout / schema → `rules_fallback` veya HOLD — **asla blind order**
- Gate `allowed=false` → stdout **GATED**, emir gönderilmez
- Alımda stop zorunlu; `--slTriggerPx` / `--tpTriggerPx` emre ilişik
- Kill-switch `logs/state.json` üzerinde kalıcı
- Cutoff 19:15 yeni alım yok · hard stop 19:20

## Çalıştırma

```bash
cd ~/trading-agent
# .env: ANTHROPIC_API_KEY=...
.venv/bin/python -m agent.main --decision auto
bash scripts/run_dashboard.sh          # local console
.venv/bin/python -m tools.metrics      # submission dump
.venv/bin/python -m pytest -q          # 40 passed
```

OKX: `okx` CLI 1.4.6, `site=tr`, spot only.

## Güvenlik

`.env`, `~/.okx`, `logs/`, `.gh_token` commit edilmez. Demo sonrası key rotate.
