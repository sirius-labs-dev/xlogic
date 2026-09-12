"""Risk and schedule limits for the trading agent."""

ALLOWED_PAIRS = ["BTC-USDT", "ETH-USDT"]  # bakıye TRY çıkarsa yarin degistirilecek

MAX_RISK_PER_TRADE = 0.005  # islem basina kasanin %0.5'i: (giris - stop) * miktar
MAX_POSITION_PCT = 0.30  # tek ciftte kasanin en fazla %30'u
MAX_TOTAL_EXPOSURE = 0.60  # toplam coin pozisyonu kasanin en fazla %60'i
DAILY_LOSS_LIMIT = 0.02  # gun basindan %2 zarar -> KILL SWITCH
MAX_ORDERS_PER_HOUR = 6
MAX_SPREAD_BPS = 10
CUTOFF_NEW_BUYS = "19:15"  # sonrasinda yeni alim yok, sadece kapatma
HARD_STOP = "19:20"

# Chat model id (provider/base URL come from .env only — never hardcode gateways).
LLM_MODEL = "claude-sonnet-4-6"

# Dry-run fake starting equity when balance API unavailable (no OKX key yet).
DRY_RUN_EQUITY = 1000.0

# Quote currency assumed for ALLOWED_PAIRS (USDT pairs).
QUOTE_CCY = "USDT"

# Decision source for agent loop:
#   "auto"  — try LLM; on auth/fail fall back to signal rules (best for Cursor /loop)
#   "llm"   — LLM only (fail → HOLD)
#   "rules" — never call LLM; signals-only (works without API key)
DECISION_MODE = "auto"
