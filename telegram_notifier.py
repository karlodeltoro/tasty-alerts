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


def _post_private(payload: dict, label: str) -> bool:
    """Igual a _post() pero envía al chat privado (TELEGRAM_PRIVATE_CHAT_ID)."""
    if not config.TELEGRAM_PRIVATE_CHAT_ID:
        logger.warning(f"TELEGRAM_PRIVATE_CHAT_ID no configurado — mensaje '{label}' no enviado")
        return False
    payload = {**payload, "chat_id": config.TELEGRAM_PRIVATE_CHAT_ID}
    return _post(payload, label)


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


def send_message(text: str) -> bool:
    """Envía un mensaje de texto libre a Telegram."""
    return _post(
        {"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        "send_message",
    )


def send_sweep_burst(
    direction: str,          # "CALL" o "PUT"
    contracts: list[dict],   # [{strike, vol_1min, delta, bid, ask, expiry_date}, ...]
    expiry_date: date,
) -> bool:
    """Envía alerta de Sweep Burst agrupada a Telegram. Retorna True si fue exitoso."""
    sorted_contracts = sorted(contracts, key=lambda c: c["strike"])
    total_vol = sum(c["vol_1min"] for c in contracts)
    n = len(contracts)
    cp  = "C" if direction == "CALL" else "P"
    dot = "🟢" if direction == "CALL" else "🔴"
    dte = (expiry_date - date.today()).days
    exp_str = expiry_date.strftime("%-d %b %Y")
    title = f"🌊 SWEEP BURST  /ES  {dot} {direction}  {total_vol} vol  {n} strikes"
    now_hms = datetime.now(_ET).strftime("%H:%M:%S")

    def _side(bid, ask, price):
        if price <= bid:   return "BID"
        if price >= ask:   return "ASK"
        return "MID"

    contract_lines = "\n".join(
        f"{int(c['strike'])}{cp}   \u0394 {abs(c['delta']):.2f}   {c['vol_1min']} vol   ${c.get('last_price', 0.0):.2f}  {_side(c['bid'], c['ask'], c.get('last_price', 0.0))}"
        for c in sorted_contracts
    )

    text = (
        f"*{title}*\n"
        f"\n"
        f"{exp_str}  |  {dte} DTE\n"
        f"{_SEP}\n"
        f"{contract_lines}\n"
        f"{_SEP}\n"
        f"{now_hms} ET  |  via BullCore"
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
    exec_price: float,
    delta: float,
    iv: float,
    vol_delta: int,
) -> bool:
    """Envía alerta de Block Print a Telegram. Retorna True si fue exitoso."""
    dte     = (expiry_date - date.today()).days
    iv_pct  = int(round(iv * 100))
    dot     = "🟢" if direction == "CALL" else "🔴"
    label   = f"{int(strike)}{dot}"
    exp_str = expiry_date.strftime("%-d %b %Y")
    now_hms = datetime.now(_ET).strftime("%H:%M:%S")

    if exec_price <= bid:    side = "BID"
    elif exec_price >= ask:  side = "ASK"
    else:                    side = "MID"

    text = (
        f"*🖨️ BLOCK PRINT  {label}  {direction}  /ES  {vol_delta} vol*\n"
        f"\n"
        f"{exp_str}  |  {dte} DTE  |  \u0394 {abs(delta):.2f}  |  IV {iv_pct}%\n"
        f"{_SEP}\n"
        f"Bid {bid:.2f}  |  Ask {ask:.2f}  |  Exec ${exec_price:.2f}\n"
        f"{_SEP}\n"
        f"{vol_delta} contratos en el {side}\n"
        f"{now_hms} ET  |  via BullCore"
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
    dte     = (expiry_date - date.today()).days
    iv_pct  = int(round(iv * 100))
    dot     = "🟢" if direction == "CALL" else "🔴"
    label   = f"{int(strike)}{dot}"
    exp_str = expiry_date.strftime("%-d %b %Y")
    now_hms = datetime.now(_ET).strftime("%H:%M:%S")
    title   = f"🔥 PRESSURE COOKER  {label}  /ES  {vol_accumulated} vol  {minutes}m"

    def _side(price):
        if price <= bid:   return "BID"
        if price >= ask:   return "ASK"
        return "MID"

    # Tape — últimas 10 transacciones
    if tape:
        tape_lines = "\n".join(
            f"{ts.strftime('%H:%M:%S')}    {size} @ ${price:.2f}  {_side(price)}"
            for ts, size, price in tape[-10:]
        )
    else:
        tape_lines = "Sin datos de tape"

    text = (
        f"*{title}*\n"
        f"\n"
        f"{exp_str}  |  {dte} DTE  |  \u0394 {abs(delta):.2f}  |  IV {iv_pct}%\n"
        f"{_SEP}\n"
        f"Tape  {minutes}m\n"
        f"{tape_lines}\n"
        f"{_SEP}\n"
        f"Total  {vol_accumulated} contratos\n"
        f"{now_hms} ET  |  via BullCore"
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
        f"{_now_et()}  |  via BullCore"
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


def send_session_renewed(expiration: object, railway_updated: bool) -> None:
    """Notifica renovacion exitosa de sesion — va al chat privado."""
    railway_str = "Railway actualizado" if railway_updated else "Railway NO actualizado — actualizar manualmente"
    text = (
        f"🔑 *Sesion Tastytrade renovada*\n"
        f"Expira: {expiration}\n"
        f"{railway_str}"
    )
    _post_private({"chat_id": "", "text": text, "parse_mode": "Markdown"}, "session renewed")


def send_session_renewal_failed(error: str) -> None:
    """Alerta de fallo en renovacion de sesion — va al chat privado."""
    text = (
        f"❌ *Fallo renovacion sesion Tastytrade*\n"
        f"Error: {error}\n\n"
        f"Ejecuta manualmente desde Mac:\n"
        f"`python renew_session.py`\n"
        f"y actualiza TT_SESSION_JSON en Railway."
    )
    _post_private({"chat_id": "", "text": text, "parse_mode": "Markdown"}, "session renewal failed")


def send_shutdown_message() -> None:
    """Notifica al apagar el sistema — va al chat privado."""
    _post_private(
        {"chat_id": "", "text": "⛔ *Sistema de alertas detenido*", "parse_mode": "Markdown"},
        "shutdown",
    )


def send_session_json_manual_update(new_b64: str, expiration: object) -> None:
    """Cuando Railway no se actualiza automáticamente, envía el base64 por Telegram en chunks."""
    warning_text = (
        f"⚠️ *Railway NO actualizado — acción requerida*\n"
        f"Sesión renovada en memoria. Expira: {expiration}\n\n"
        f"Copia el valor del siguiente mensaje y pégalo en\n"
        f"Railway → Variables → TT_SESSION_JSON → Redeploy"
    )
    try:
        httpx.post(f"{_TELEGRAM_BASE}/sendMessage",
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": warning_text, "parse_mode": "Markdown"},
            timeout=10).raise_for_status()
    except Exception as e:
        logger.error(f"Telegram manual update warning error: {e}")
    for idx, chunk in enumerate([new_b64[i:i+3800] for i in range(0, len(new_b64), 3800)], 1):
        try:
            httpx.post(f"{_TELEGRAM_BASE}/sendMessage",
                json={"chat_id": config.TELEGRAM_CHAT_ID,
                      "text": f"TT_SESSION_JSON:\n\n`{chunk}`",
                      "parse_mode": "Markdown"},
                timeout=10).raise_for_status()
        except Exception as e:
            logger.error(f"Telegram session_json chunk {idx} error: {e}")
