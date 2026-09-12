# XLogic / trading-agent

**Komünite × OKX TR Agentic Trading Hackathon — 12 Eylül 2026**

Fail-closed otonom spot ajanı: gözlem → sinyal → LLM/rules → **risk kapısı** → OKX CLI emir → journal.

> Teslim paketi: [`submission/`](./submission/) · Canlı UX: http://127.0.0.1:8787

## Durum (canlı)

- OKX TR spot (`okx` CLI 1.4.6, profil `furkan`, `site=tr`)
- Döngü: `python -m agent.main --decision auto` (Anthropic) veya `--decision rules`
- İlk canlı fill: BUY ETH-USDT · `clOrdId` prefix `agt…`
- XLogic konsol: `bash scripts/run_dashboard.sh`

## Hızlı çalıştırma

```bash
cd ~/trading-agent
# .env: ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python -m agent.main --decision auto          # canlı loop
.venv/bin/python -m agent.main --once --decision rules  # tek cycle
bash scripts/run_dashboard.sh                           # UX :8787
.venv/bin/python -m tools.metrics                       # submission dump
.venv/bin/python -m pytest -q
```

## Mimari

```
okx --json  →  signals (EMA/ATR)  →  LLM | rules  →  risk_gate  →  spot place (+SL/TP)  →  journal
```

| Modül | Görev |
|-------|--------|
| `tools/okx_client.py` | Subprocess CLI; imza yok |
| `tools/signals.py` | Trend / entry / exit |
| `tools/llm.py` | Anthropic Messages (öncelik) veya OpenAI-compatible |
| `tools/rule_decision.py` | Fail-closed rules |
| `tools/risk_gate.py` | Boyut + 13 red kodu + kill-switch |
| `tools/journal.py` | `logs/decisions.jsonl`, `logs/equity.csv` |
| `tools/metrics.py` | Performans dump |
| `dashboard/` | XLogic live console |
| `config.py` | Limitler / çiftler / cutoff |

## Risk (özet)

`MAX_RISK_PER_TRADE=0.5%` · position 30% · exposure 60% · daily loss 2% → kill · 6 emir/saat · spread 10 bps · cutoff 19:15 · hard stop 19:20.

Miktarı LLM belirlemez; `risk_gate` hesaplar. Stop yoksa alım yok.

## Önceden hazırlanan araçlar

Etkinlik öncesi / gün içi iskelet: `okx_client`, `risk_gate`+testler, `signals`+testler, `llm`+testler, ajan döngüsü, journal, XLogic dashboard, metrics — saklanmadan repo’da.

## Güvenlik

`.env` ve `~/.okx/config.toml` commit edilmez. Secret’ları chat’e yapıştırmayın.
