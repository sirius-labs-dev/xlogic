# Cursor /loop ile trading-agent

## Mimari

```
Cursor /loop (zamanlayıcı)  →  scripts/loop_tick.sh  →  agent/main.py --once
                                      ↓
                         sinyaller → (rules|llm) → risk_gate → okx CLI
```

- **Emirleri Cursor sohbeti vermez** — sadece script çalıştırır (`clOrdId=agt…`).
- LLM key yoksa `--decision rules` ile sinyal kuralları karar verir.
- Otonomi kanıtı script loglarında kalır.

## Başlatma (Cursor Agent sohbetine)

Dry-run (emir yok):

```
/loop 1m Bu tick'te SADECE şunu çalıştır, başka bir şey yapma, emir uydurma:
cd ~/trading-agent && bash scripts/loop_tick.sh
Çıktının son satırını (kasa özeti) kısaca göster.
```

Canlı (OKX login + bakiye hazırken):

```
/loop 1m SADECE şunu çalıştır:
cd ~/trading-agent && bash scripts/loop_tick.sh --live
Özeti bir satır yaz. Emir parametresi uydurma.
```

Durdurma: sohbete `stop the loop` / `/loop` durdur.

## Karar modları

| Mod | Flag | Ne zaman |
|-----|------|----------|
| `rules` | `--decision rules` (loop_tick varsayılanı) | LLM key yok / Cortex 401 |
| `auto` | `--decision auto` | Key var; fail olursa rules'a düş |
| `llm` | `--decision llm` | Sadece LLM; fail → HOLD |

## Manuel tek tick

```bash
cd ~/trading-agent
bash scripts/loop_tick.sh              # dry-run + rules
.venv/bin/python -m agent.main --once --dry-run --decision rules
.venv/bin/python -m agent.main --once --decision auto   # LLM dene
```
