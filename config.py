"""
config.py — Carga y valida todas las variables de entorno.
Todos los demás módulos importan desde aquí, nunca directo de os.environ.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(
            f"Variable requerida '{key}' no encontrada en .env\n"
            f"Copia .env.example → .env y rellena los valores."
        )
    return val


# ── Tastytrade ────────────────────────────────────────────────
TT_USERNAME       = _require("TT_USERNAME")
TT_PASSWORD       = _require("TT_PASSWORD")

# ── Railway (para renovacion automatica de sesion) ────────────
# RAILWAY_API_TOKEN:      railway.app → Account Settings → Tokens → Create Token
# RAILWAY_PROJECT_ID:     railway.app → Project → Settings → General → Project ID
# RAILWAY_SERVICE_ID:     railway.app → Project → Service → Settings → Service ID
# RAILWAY_ENVIRONMENT_ID: railway.app → Project → Environments → (hover env) → Copy ID
RAILWAY_API_TOKEN      = os.getenv("RAILWAY_API_TOKEN")
RAILWAY_PROJECT_ID     = os.getenv("RAILWAY_PROJECT_ID")
RAILWAY_SERVICE_ID     = os.getenv("RAILWAY_SERVICE_ID")
RAILWAY_ENVIRONMENT_ID = os.getenv("RAILWAY_ENVIRONMENT_ID")

# ── Telegram ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN      = _require("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID        = _require("TELEGRAM_CHAT_ID")
TELEGRAM_PRIVATE_CHAT_ID = os.getenv("TELEGRAM_PRIVATE_CHAT_ID", "")

# ── Sweep Burst ───────────────────────────────────────────────
SWEEP_BURST_WINDOW_SECONDS  = int(os.getenv("SWEEP_BURST_WINDOW_SECONDS", "60"))
SWEEP_BURST_WINDOW_2MIN     = int(os.getenv("SWEEP_BURST_WINDOW_2MIN", "120"))
SWEEP_BURST_MIN_CONTRACTS   = int(os.getenv("SWEEP_BURST_MIN_CONTRACTS", "5"))
SWEEP_BURST_MIN_VOL         = int(os.getenv("SWEEP_BURST_MIN_VOL", "50"))
SWEEP_BURST_MIN_CONTRACTS_B = int(os.getenv("SWEEP_BURST_MIN_CONTRACTS_B", "3"))
SWEEP_BURST_MIN_VOL_B       = int(os.getenv("SWEEP_BURST_MIN_VOL_B", "50"))
MIN_DELTA                   = float(os.getenv("MIN_DELTA", "0.30"))
MAX_DELTA                   = float(os.getenv("MAX_DELTA", "0.95"))
FALLBACK_VOL                = float(os.getenv("FALLBACK_VOL", "0.20"))
ALERT_COOLDOWN_SECONDS      = int(os.getenv("ALERT_COOLDOWN_SECONDS", "120"))
# Con streaming real no hay cache dumps — cap solo como seguridad.
MAX_VOL_DELTA_PER_CYCLE     = int(os.getenv("MAX_VOL_DELTA_PER_CYCLE", "500"))

# ── Block Print ───────────────────────────────────────────────
BLOCK_PRINT_MIN_VOL          = int(os.getenv("BLOCK_PRINT_MIN_VOL", "100"))
# ── Block Print aggressiveness filter ─────────────────────────
BLOCK_PRINT_SMALL_MIN_VOL    = int(os.getenv("BLOCK_PRINT_SMALL_MIN_VOL", "100"))
BLOCK_PRINT_MEDIUM_MIN_VOL   = int(os.getenv("BLOCK_PRINT_MEDIUM_MIN_VOL", "150"))
BLOCK_PRINT_MIN_DELTA        = float(os.getenv("BLOCK_PRINT_MIN_DELTA", "0.40"))
BLOCK_PRINT_MAX_DELTA        = float(os.getenv("BLOCK_PRINT_MAX_DELTA", "0.90"))
BLOCK_PRINT_COOLDOWN_SECONDS = int(os.getenv("BLOCK_PRINT_COOLDOWN_SECONDS", "120"))
# Horario dual: mercado (9:01–17:59 ET) vs fuera de mercado (18:00–9:00 ET)
BLOCK_PRINT_MARKET_MIN_VOL        = int(os.getenv("BLOCK_PRINT_MARKET_MIN_VOL", "200"))
BLOCK_PRINT_MARKET_MIN_DELTA      = float(os.getenv("BLOCK_PRINT_MARKET_MIN_DELTA", "0.40"))
BLOCK_PRINT_AFTER_HOURS_MIN_VOL   = int(os.getenv("BLOCK_PRINT_AFTER_HOURS_MIN_VOL", "100"))
BLOCK_PRINT_AFTER_HOURS_MIN_DELTA = float(os.getenv("BLOCK_PRINT_AFTER_HOURS_MIN_DELTA", "0.30"))

# ── Pressure Cooker ───────────────────────────────────────────
PRESSURE_COOKER_2MIN_VOL         = int(os.getenv("PRESSURE_COOKER_2MIN_VOL", "500"))
PRESSURE_COOKER_5MIN_VOL         = int(os.getenv("PRESSURE_COOKER_5MIN_VOL", "1000"))
PRESSURE_COOKER_COOLDOWN_SECONDS = int(os.getenv("PRESSURE_COOKER_COOLDOWN_SECONDS", "120"))

# ── Filtros globales de contrato ───────────────────────────────
MIN_CONTRACT_PRICE      = float(os.getenv("MIN_CONTRACT_PRICE", "7.00"))
MAX_STRIKE_DISTANCE_PCT = float(os.getenv("MAX_STRIKE_DISTANCE_PCT", "0.15"))

# ── Subyacentes ───────────────────────────────────────────────
WATCH_SYMBOLS: list[str] = [
    s.strip() for s in os.getenv("WATCH_SYMBOLS", "/ES").split(",") if s.strip()
]

# ── Modo Raw ──────────────────────────────────────────────────
RAW_ALERT_MODE       = os.getenv("RAW_ALERT_MODE", "false").lower() == "true"
RAW_MIN_PRICE        = float(os.getenv("RAW_MIN_PRICE", "10.00"))
RAW_MIN_VOL_1MIN     = int(os.getenv("RAW_MIN_VOL_1MIN", "50"))
RAW_COOLDOWN_SECONDS = int(os.getenv("RAW_COOLDOWN_SECONDS", "120"))

# ── Block Accumulator ─────────────────────────────────────────
BLOCK_ACCUM_MIN_VOL  = int(os.getenv("BLOCK_ACCUM_MIN_VOL", "300"))

# ── Contract price floor ──────────────────────────────────────
MIN_CONTRACT_PRICE_FLOOR = float(os.getenv("MIN_CONTRACT_PRICE_FLOOR", "2.0"))

# ── Stream warmup / pending trades ───────────────────────────
QUOTE_WARMUP_SECONDS          = float(os.getenv("QUOTE_WARMUP_SECONDS", "2.0"))
PENDING_TRADE_MAX_AGE_SECONDS = float(os.getenv("PENDING_TRADE_MAX_AGE_SECONDS", "30.0"))
LARGE_TRADE_BYPASS_SIZE       = int(os.getenv("LARGE_TRADE_BYPASS_SIZE", "200"))

# ── Stream watchdog ───────────────────────────────────────────
WATCHDOG_FREEZE_SECONDS = int(os.getenv("WATCHDOG_FREEZE_SECONDS", "180"))
WATCHDOG_CHECK_INTERVAL = int(os.getenv("WATCHDOG_CHECK_INTERVAL", "60"))

# ── Multi-expiry support ──────────────────────────────────────
MULTI_EXPIRY_COUNT = int(os.getenv("MULTI_EXPIRY_COUNT", "2"))

# ── Schwab MacroContext ───────────────────────────────────────
SCHWAB_CLIENT_ID     = os.getenv("SCHWAB_CLIENT_ID", "")
SCHWAB_CLIENT_SECRET = os.getenv("SCHWAB_CLIENT_SECRET", "")
SCHWAB_TOKEN_JSON    = os.getenv("SCHWAB_TOKEN_JSON", "")
SCHWAB_TOKEN_PATH    = os.getenv("SCHWAB_TOKEN_PATH", "")
SCHWAB_ENABLED       = os.getenv("SCHWAB_ENABLED", "false").lower() == "true"
SCHWAB_CONTRACT_MONITOR_ENABLED = os.getenv("SCHWAB_CONTRACT_MONITOR_ENABLED", "true").lower() == "true"
SCHWAB_VELOCITY_1M_THRESHOLD    = float(os.getenv("SCHWAB_VELOCITY_1M_THRESHOLD", "40"))   # contracts/min to flag candidate
SCHWAB_VELOCITY_ACCELERATION    = float(os.getenv("SCHWAB_VELOCITY_ACCELERATION", "2.0"))   # velocity_1m must be N× velocity_5m
SCHWAB_VOL_OI_MIN_RATIO         = float(os.getenv("SCHWAB_VOL_OI_MIN_RATIO", "0.25"))       # min dayVol/OI ratio for surge detection

# ── API server ────────────────────────────────────────────────
API_ENABLED = os.getenv("API_ENABLED", "true").lower() == "true"
API_PORT    = int(os.getenv("PORT", os.getenv("API_PORT", "8000")))
API_KEY     = os.getenv("BULLCORE_API_KEY", "")  # empty = open access (dev mode)
