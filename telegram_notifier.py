"""
telegram_notifier.py — Envía mensajes de alerta a Telegram.

Usa httpx (síncrono) para no depender de python-telegram-bot.
"""
import logging
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

import config

_ET = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)

_TELEGRAM_BASE = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"

_SEP = "─" * 21
_MAX_RETRIES = 3
_RETRY_DELAY = 2  # segundos entre reintentos


def _post(payload: dict, label: str) -> bool:
    """Envía un mensaje a Telegram con reintentos automáticos."""
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = httpx.post(
                f"{_TELEGRAM_BASE}/sendMessage",
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            return True
        except httpx.HTTPError as e:
            if attempt < _MAX_RETRIES:
                logger.warning(f"Telegram {label} intento {attempt} fallido: {e} — reintentando en {_RETRY_DELAY}s")
                time.sleep(_RETRY_DELAY)
            else:
                logger.error(f"Telegram {label} error tras {_MAX_RETRIES} intentos: {e}")
    return False


def _fmt_exp(expiry_date: date) -> str:
    return expiry_date.strftime("%-d %b %Y")


def _now_et() -> str:
    return datetime.now(_ET).strftime("%H:%M:%S") + " ET"


def send_sweep_burst(
    direction: str,          # "CALL" o "PUT"
    contracts: list[dict],   # [{strike, vol_1min, delta, bid, ask, expiry_date}, ...]
    expiry_date: date,
) -> bool:
    """Envía alerta de Sweep Burst agrupada a Telegram. Retorna True si fue exitoso."""
    sorted_contracts = sorted(contracts, key=lambda c: c["strike"])
    total_vol = sum(c["vol_1min"] for c in contracts)
    n = len(contracts)
    cp = "C" if direction == "CALL" else "P"
    dot = "🟢" if direction == "CALL" else "🔴"
    dte = (expiry_date - date.today()).days
    title = f"\u25c6 SWEEP  /ES  {dot} {direction}  {total_vol} vol  {n} strikes"

    contract_lines = "\n".join(
        f"{int(c['strike'])}{cp}   \u0394 {abs(c['delta']):.2f}   {c['vol_1min']} vol"
        for c in sorted_contracts
    )

    text = (
        f"*{title}*\n"
        f"\n"
        f"{title}\n"
        f"\n"
        f"{dte} DTE\n"
        f"{_SEP}\n"
        f"{contract_lines}\n"
        f"{_SEP}\n"
        f"ET  |  via Tastytrade"
    )

    ok = _post({"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, "sweep burst")
    if ok:
        logger.info(f"Telegram SWEEP BURST OK: {direction} {n} contratos")
    return ok


def send_block_print(
    direction: str,       # "CALL" o "PUT"
    strike: float,
    expiry_date: date,
    bid: float,
    ask: float,
    last_price: float,
    delta: float,
    iv: float,
    vol_delta: int,
) -> bool:
    """Envía alerta de Block Print a Telegram. Retorna True si fue exitoso."""
    dte    = (expiry_date - date.today()).days
    iv_pct = int(round(iv * 100))
    mark   = (bid + ask) / 2 if (bid + ask) > 0 else last_price
    cp     = "C" if direction == "CALL" else "P"
    dot    = "🟢" if direction == "CALL" else "🔴"
    label  = f"{int(strike)}{dot}"
    now_hms = datetime.now(_ET).strftime("%H:%M:%S")

    text = (
        f"*\u25a3 BLOCK  {label}  /ES  {vol_delta} vol*\n"
        f"\n"
        f"{label}  /ES\n"
        f"{dte} DTE\n"
        f"Bid {bid:.2f}  Ask {ask:.2f}  Mark {mark:.2f}\n"
        f"\u0394 {abs(delta):.2f}  IV {iv_pct}%\n"
        f"{_SEP}\n"
        f"{now_hms}    {vol_delta} @ ${mark:.2f}\n"
        f"{_SEP}\n"
        f"ET  |  via Tastytrade"
    )

    ok = _post({"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, "block print")
    if ok:
        logger.info(f"Telegram BLOCK PRINT OK: {direction} strike {int(strike)} vol {vol_delta}")
    return ok


def send_pressure_cooker(
    minutes: int,         # 2 o 5
    direction: str,       # "CALL" o "PUT"
    strike: float,
    expiry_date: date,
    bid: float,
    ask: float,
    last_price: float,
    delta: float,
    iv: float,
    vol_accumulated: int,
    tape: list,           # [(timestamp, size, price), ...]
) -> bool:
    """Envía alerta de Pressure Cooker a Telegram. Retorna True si fue exitoso."""
    dte      = (expiry_date - date.today()).days
    iv_pct   = int(round(iv * 100))
    mark     = (bid + ask) / 2 if (bid + ask) > 0 else last_price
    arrow    = "\u25b2" if direction == "CALL" else "\u25bc"
    cp       = "C" if direction == "CALL" else "P"
    dot      = "🟢" if direction == "CALL" else "🔴"
    label    = f"{int(strike)}{dot}"
    title    = f"{arrow} FLOW  {label}  /ES  {vol_accumulated} vol  {minutes}m"

    # Tape — últimas 10 transacciones
    tape_lines = ""
    if tape:
        shown = tape[-10:]
        tape_lines = "\n".join(
            f"{ts.strftime('%H:%M:%S')}    {size} @ ${price:.2f}"
            for ts, size, price in shown
        )
    else:
        tape_lines = "Sin datos de tape"

    text = (
        f"*{title}*\n"
        f"\n"
        f"{label}  /ES\n"
        f"{dte} DTE\n"
        f"Bid {bid:.2f}  Ask {ask:.2f}  Mark {mark:.2f}\n"
        f"\u0394 {abs(delta):.2f}  IV {iv_pct}%\n"
        f"{_SEP}\n"
        f"Tape  {minutes}m\n"
        f"{tape_lines}\n"
        f"{_SEP}\n"
        f"Total  {vol_accumulated} contratos\n"
        f"ET  |  via Tastytrade"
    )

    ok = _post({"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, f"pressure cooker {minutes}min")
    if ok:
        logger.info(f"Telegram PRESSURE COOKER {minutes}min OK: {direction} strike {int(strike)} vol {vol_accumulated}")
    return ok


def send_raw_alert(
    direction: str,
    strike: float,
    expiry_date: date,
    vol_1min: int,
    mark: float,
) -> bool:
    """Alerta modo raw: contrato > $10 con ≥ 50 vol en 60s (lastSize reales)."""
    dte  = (expiry_date - date.today()).days
    text = (
        f"*\u25cf RAW  /ES  {direction}  {vol_1min} vol  1m*\n"
        f"{_SEP}\n"
        f"Strike: {int(strike)}  |  Exp: {_fmt_exp(expiry_date)}  |  {dte} DTE\n"
        f"Mark: ${mark:.2f}\n"
        f"{_SEP}\n"
        f"{_now_et()}  |  via Tastytrade"
    )
    ok = _post({"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, "raw")
    if ok:
        logger.info(f"Telegram RAW OK: {direction} strike {int(strike)} vol {vol_1min} mark ${mark:.2f}")
    return ok


_SEP_THICK = "\u2501" * 20  # ━━━━━━━━━━━━━━━━━━━━


def send_startup_message(symbol_count: int, expiry_date: date | None) -> None:
    """Notifica al arrancar el sistema — se llama UNA sola vez al inicio."""
    now_et  = datetime.now(_ET)
    now_str = now_et.strftime("%I:%M %p ET").lstrip("0")
    dte     = (expiry_date - date.today()).days if expiry_date else "?"
    exp_str = str(expiry_date) if expiry_date else "?"
    underlying = ", ".join(config.WATCH_SYMBOLS)

    text = (
        f"🚀 *BullCore Capital V1 — EN VIVO*\n"
        f"{_SEP_THICK}\n"
        f"📡 Monitoreando: *{symbol_count}* contratos {underlying}\n"
        f"📅 Expiración activa: {exp_str} ({dte} DTE)\n"
        f"🔺 Delta mínimo: ≥{config.MIN_DELTA}\n"
        f"\n"
        f"🎯 *Filtros activos:*\n"
        f"🌊 Sweep Burst — {config.SWEEP_BURST_MIN_CONTRACTS}+ contratos "
        f"≥{config.SWEEP_BURST_MIN_VOL} vol en 60s\n"
        f"🖨️ Block Print — 1 transacción ≥{config.BLOCK_PRINT_MIN_VOL} contratos\n"
        f"🔥 Pressure Cooker — "
        f"≥{config.PRESSURE_COOKER_2MIN_VOL} vol/2min · "
        f"≥{config.PRESSURE_COOKER_5MIN_VOL} vol/5min\n"
        f"\n"
        f"{_SEP_THICK}\n"
        f"⏰ Inicio: {now_str}"
    )
    try:
        httpx.post(
            f"{_TELEGRAM_BASE}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        ).raise_for_status()
    except httpx.HTTPError as e:
        logger.error(f"Telegram startup msg error: {e}")


def send_reload_message(symbol_count: int, expiry_date: date | None) -> None:
    """Notifica el reload diario de la cadena (17:15 ET)."""
    exp_str = str(expiry_date) if expiry_date else "?"
    dte     = (expiry_date - date.today()).days if expiry_date else "?"
    now_str = datetime.now(_ET).strftime("%I:%M %p ET").lstrip("0")
    text = (
        f"🔄 *Cadena recargada — nueva sesión*\n"
        f"📡 {symbol_count} contratos /ES activos\n"
        f"📅 Nueva expiración: {exp_str} ({dte} DTE)\n"
        f"⏰ {now_str}"
    )
    try:
        httpx.post(
            f"{_TELEGRAM_BASE}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        ).raise_for_status()
    except httpx.HTTPError as e:
        logger.error(f"Telegram reload msg error: {e}")


def send_stream_verification_message(symbols: list[str]) -> None:
    """Confirmación 18:05 ET — stream activo con los contratos de la nueva sesión."""
    count = len(symbols)
    now_str = datetime.now().strftime("%H:%M:%S")
    text = (
        f"✅ *Stream verificado (18:05 ET)*\n"
        f"🎯 *{count}* contratos activos para la sesión nocturna\n"
        f"📌 Subyacentes: `{', '.join(config.WATCH_SYMBOLS)}`\n"
        f"🕐 {now_str}"
    )
    try:
        httpx.post(
            f"{_TELEGRAM_BASE}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        ).raise_for_status()
    except httpx.HTTPError as e:
        logger.error(f"Telegram verify msg error: {e}")


def send_shutdown_message() -> None:
    """Notifica al apagar el sistema."""
    try:
        httpx.post(
            f"{_TELEGRAM_BASE}/sendMessage",
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": "⛔ *Sistema de alertas detenido*",
                "parse_mode": "Markdown",
            },
            timeout=10,
        ).raise_for_status()
    except httpx.HTTPError:
        pass
