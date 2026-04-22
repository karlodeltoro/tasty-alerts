"""
telegram_notifier.py — Envía mensajes de alerta a Telegram.

Usa httpx.AsyncClient para no bloquear el event loop de asyncio.

Funciones sync (send_session_*  y send_shutdown_message) se mantienen
síncronas porque renew_session.py corre en un executor thread que no
tiene un event loop activo y no puede hacer await.
"""
import asyncio
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

import config

_ET = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

_TELEGRAM_BASE = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}"
_TG_URL        = f"{_TELEGRAM_BASE}/sendMessage"
_TIMEOUT       = 10
_SEP           = "─" * 21
_MAX_RETRIES   = 3
_RETRY_DELAY   = 2  # segundos entre reintentos


# ── Sync helpers (renew_session.py / _shutdown — sin event loop) ──────────────

def _post_sync(payload: dict, label: str) -> bool:
    import time
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            resp = httpx.post(_TG_URL, json=payload, timeout=_TIMEOUT)
            resp.raise_for_status()
            return True
        except httpx.HTTPError as e:
            if attempt < _MAX_RETRIES:
                logger.warning(
                    f"Telegram {label} intento {attempt} fallido: {e} "
                    f"— reintentando en {_RETRY_DELAY}s"
                )
                time.sleep(_RETRY_DELAY)
            else:
                logger.error(f"Telegram {label} error tras {_MAX_RETRIES} intentos: {e}")
    return False


def _post_private_sync(payload: dict, label: str) -> bool:
    if not config.TELEGRAM_PRIVATE_CHAT_ID:
        logger.warning(
            f"TELEGRAM_PRIVATE_CHAT_ID no configurado — mensaje '{label}' no enviado"
        )
        return False
    payload = {**payload, "chat_id": config.TELEGRAM_PRIVATE_CHAT_ID}
    return _post_sync(payload, label)


# ── Async helpers ─────────────────────────────────────────────────────────────

async def _post(payload: dict, label: str = "") -> bool:
    for attempt in range(_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                r = await client.post(_TG_URL, json=payload)
            if r.status_code == 200:
                return True
            logger.warning(
                f"Telegram {label} HTTP {r.status_code} "
                f"(attempt {attempt+1}/{_MAX_RETRIES})"
            )
        except Exception as e:
            logger.warning(
                f"Telegram {label} error: {e} "
                f"(attempt {attempt+1}/{_MAX_RETRIES})"
            )
        if attempt < _MAX_RETRIES - 1:
            await asyncio.sleep(_RETRY_DELAY)
    logger.error(f"Telegram {label} failed after {_MAX_RETRIES} attempts")
    return False


async def _post_private(payload: dict, label: str) -> bool:
    if not config.TELEGRAM_PRIVATE_CHAT_ID:
        logger.warning(
            f"TELEGRAM_PRIVATE_CHAT_ID no configurado — mensaje '{label}' no enviado"
        )
        return False
    payload = {**payload, "chat_id": config.TELEGRAM_PRIVATE_CHAT_ID}
    return await _post(payload, label)


# ── Formatters ────────────────────────────────────────────────────────────────

def _fmt_exp(expiry_date: date) -> str:
    return expiry_date.strftime("%-d %b %Y")


def _now_et() -> str:
    return datetime.now(_ET).strftime("%H:%M:%S") + " ET"


# ── Mensajes genéricos ────────────────────────────────────────────────────────

async def send_message(text: str) -> bool:
    """Envía un mensaje de texto libre al canal de alertas."""
    return await _post(
        {"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        "send_message",
    )


async def send_private_message(text: str) -> bool:
    """Envía un mensaje de texto libre al chat privado (sistema/errores)."""
    return await _post_private(
        {"text": text, "parse_mode": "Markdown"},
        "send_private_message",
    )


# ── Alertas de trading (async) ────────────────────────────────────────────────

async def send_sweep_burst(
    direction: str,
    contracts: list[dict],
    expiry_date: date,
    underlying: str = "/ES",
    macro_context: str = "",
) -> bool:
    """Envía alerta de Sweep Burst agrupada a Telegram."""
    sorted_contracts = sorted(contracts, key=lambda c: c["strike"])
    total_vol = sum(c["vol_1min"] for c in contracts)
    n   = len(contracts)
    cp  = "C" if direction == "CALL" else "P"
    dot = "🟢" if direction == "CALL" else "🔴"
    dte = (expiry_date - date.today()).days
    exp_str = expiry_date.strftime("%-d %b %Y")
    title   = f"🌊 SWEEP BURST  {underlying}  {dot} {direction}  {total_vol} vol  {n} strikes"
    now_hms = datetime.now(_ET).strftime("%H:%M:%S")

    def _side(bid, ask, price):
        if price <= bid: return "BID"
        if price >= ask: return "ASK"
        return "MID"

    contract_lines = []
    for c in sorted_contracts:
        line = (
            f"{int(c['strike'])}{cp}   Δ {abs(c['delta']):.2f}   {c['vol_1min']} vol"
            f"   ${c.get('last_price', 0.0):.2f}  "
            f"{_side(c['bid'], c['ask'], c.get('last_price', 0.0))}"
        )
        ask_ratio = c.get("ask_ratio", 0.0)
        if ask_ratio >= 0.6:
            line += f"  {int(ask_ratio * 100)}% ask-side"
        contract_lines.append(line)

    macro_line = f"{macro_context}\n" if macro_context else ""
    text = (
        f"*{title}*\n"
        f"\n"
        f"{exp_str}  |  {dte} DTE\n"
        f"{_SEP}\n"
        f"{chr(10).join(contract_lines)}\n"
        f"{_SEP}\n"
        f"{macro_line}"
        f"{now_hms} ET  |  via BullCore"
    )
    ok = await _post(
        {"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        "sweep burst",
    )
    if ok:
        logger.info(f"Telegram SWEEP BURST OK: {direction} {n} contratos")
    return ok


async def send_block_print(
    direction: str,
    strike: float,
    expiry_date: date,
    bid: float,
    ask: float,
    exec_price: float,
    delta: float,
    iv: float,
    vol_delta: int,
    ask_ratio: float = 0.0,
    underlying: str = "/ES",
    macro_context: str = "",
) -> bool:
    """Envía alerta de Block Print a Telegram."""
    dte     = (expiry_date - date.today()).days
    iv_pct  = int(round(iv * 100))
    dot     = "🟢" if direction == "CALL" else "🔴"
    label   = f"{int(strike)}{dot}"
    exp_str = expiry_date.strftime("%-d %b %Y")
    now_hms = datetime.now(_ET).strftime("%H:%M:%S")

    if exec_price <= bid:   side = "BID"
    elif exec_price >= ask: side = "ASK"
    else:                   side = "MID"

    ask_line   = f"  |  {int(ask_ratio * 100)}% ask-side" if ask_ratio >= 0.6 else ""
    macro_line = f"{macro_context}\n" if macro_context else ""

    text = (
        f"*🖨️ BLOCK PRINT  {label}  {direction}  {underlying}  {vol_delta} vol*\n"
        f"\n"
        f"{exp_str}  |  {dte} DTE  |  Δ {abs(delta):.2f}  |  IV {iv_pct}%\n"
        f"{_SEP}\n"
        f"Bid {bid:.2f}  |  Ask {ask:.2f}  |  Exec ${exec_price:.2f}\n"
        f"{_SEP}\n"
        f"{vol_delta} contratos en el {side}{ask_line}\n"
        f"{macro_line}"
        f"{now_hms} ET  |  via BullCore"
    )
    ok = await _post(
        {"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        "block print",
    )
    if ok:
        logger.info(f"Telegram BLOCK PRINT OK: {direction} strike {int(strike)} vol {vol_delta}")
    return ok


async def send_block_accum(
    direction: str,
    strike: float,
    expiry_date: date,
    bid: float,
    ask: float,
    delta: float,
    iv: float,
    vol_30s: int,
    ask_ratio: float = 0.0,
    underlying: str = "/ES",
    macro_context: str = "",
) -> bool:
    """Envía alerta de Block Accumulator a Telegram."""
    dte     = (expiry_date - date.today()).days
    iv_pct  = int(round(iv * 100))
    dot     = "🟢" if direction == "CALL" else "🔴"
    label   = f"{int(strike)}{dot}"
    exp_str = expiry_date.strftime("%-d %b %Y")
    now_hms = datetime.now(_ET).strftime("%H:%M:%S")

    ask_line   = f"  |  {int(ask_ratio * 100)}% ask-side" if ask_ratio >= 0.6 else ""
    macro_line = f"{macro_context}\n" if macro_context else ""

    text = (
        f"*🏦 BLOCK ACCUM  {label}  {direction}  {underlying}  {vol_30s} vol / 30s*\n"
        f"\n"
        f"{exp_str}  |  {dte} DTE  |  Δ {abs(delta):.2f}  |  IV {iv_pct}%\n"
        f"{_SEP}\n"
        f"Bid {bid:.2f}  |  Ask {ask:.2f}\n"
        f"{_SEP}\n"
        f"{vol_30s} contratos en 30s{ask_line}\n"
        f"{macro_line}"
        f"{now_hms} ET  |  via BullCore"
    )
    ok = await _post(
        {"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        "block accum",
    )
    if ok:
        logger.info(f"Telegram BLOCK ACCUM OK: {direction} strike {int(strike)} vol_30s {vol_30s}")
    return ok


async def send_pressure_cooker(
    minutes: int,
    direction: str,
    strike: float,
    expiry_date: date,
    bid: float,
    ask: float,
    last_price: float,
    delta: float,
    iv: float,
    vol_accumulated: int,
    tape: list,
    underlying: str = "/ES",
    macro_context: str = "",
) -> bool:
    """Envía alerta de Pressure Cooker a Telegram."""
    dte     = (expiry_date - date.today()).days
    iv_pct  = int(round(iv * 100))
    dot     = "🟢" if direction == "CALL" else "🔴"
    label   = f"{int(strike)}{dot}"
    exp_str = expiry_date.strftime("%-d %b %Y")
    now_hms = datetime.now(_ET).strftime("%H:%M:%S")
    title   = f"🔥 PRESSURE COOKER  {label}  {underlying}  {vol_accumulated} vol  {minutes}m"

    def _side(price):
        if price <= bid: return "BID"
        if price >= ask: return "ASK"
        return "MID"

    if tape:
        tape_lines = "\n".join(
            f"{ts.strftime('%H:%M:%S')}    {size} @ ${price:.2f}  {_side(price)}"
            for ts, size, price in tape[-10:]
        )
    else:
        tape_lines = "Sin datos de tape"

    macro_line = f"{macro_context}\n" if macro_context else ""
    text = (
        f"*{title}*\n"
        f"\n"
        f"{exp_str}  |  {dte} DTE  |  Δ {abs(delta):.2f}  |  IV {iv_pct}%\n"
        f"{_SEP}\n"
        f"Tape  {minutes}m\n"
        f"{tape_lines}\n"
        f"{_SEP}\n"
        f"Total  {vol_accumulated} contratos\n"
        f"{macro_line}"
        f"{now_hms} ET  |  via BullCore"
    )
    ok = await _post(
        {"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        f"pressure cooker {minutes}min",
    )
    if ok:
        logger.info(
            f"Telegram PRESSURE COOKER {minutes}min OK: "
            f"{direction} strike {int(strike)} vol {vol_accumulated}"
        )
    return ok


async def send_raw_alert(
    direction: str,
    strike: float,
    expiry_date: date,
    vol_1min: int,
    mark: float,
    underlying: str = "/ES",
) -> bool:
    """Alerta modo raw: contrato > $10 con ≥ 50 vol en 60s."""
    dte  = (expiry_date - date.today()).days
    text = (
        f"*● RAW  {underlying}  {direction}  {vol_1min} vol  1m*\n"
        f"{_SEP}\n"
        f"Strike: {int(strike)}  |  Exp: {_fmt_exp(expiry_date)}  |  {dte} DTE\n"
        f"Mark: ${mark:.2f}\n"
        f"{_SEP}\n"
        f"{_now_et()}  |  via BullCore"
    )
    ok = await _post(
        {"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        "raw",
    )
    if ok:
        logger.info(
            f"Telegram RAW OK: {direction} strike {int(strike)} vol {vol_1min} mark ${mark:.2f}"
        )
    return ok


# ── Mensajes de sistema (async) ───────────────────────────────────────────────

_SEP_THICK = "━" * 20


async def send_startup_message(symbol_count: int, expiry_date: date | None) -> None:
    """Notifica al arrancar el sistema — va al chat privado."""
    now_et     = datetime.now(_ET)
    now_str    = now_et.strftime("%I:%M %p ET").lstrip("0")
    dte        = (expiry_date - date.today()).days if expiry_date else "?"
    exp_str    = str(expiry_date) if expiry_date else "?"
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
    await _post_private({"text": text, "parse_mode": "Markdown"}, "startup")


async def send_reload_message(
    symbol_count: int,
    expiry_date: date | None,
    underlying: str = "/ES",
) -> None:
    """Notifica el reload diario de la cadena — va al chat privado."""
    exp_str = str(expiry_date) if expiry_date else "?"
    dte     = (expiry_date - date.today()).days if expiry_date else "?"
    now_str = datetime.now(_ET).strftime("%I:%M %p ET").lstrip("0")
    text = (
        f"🔄 *Cadena recargada — nueva sesión*\n"
        f"📡 {symbol_count} contratos {underlying} activos\n"
        f"📅 Nueva expiración: {exp_str} ({dte} DTE)\n"
        f"⏰ {now_str}"
    )
    await _post_private({"text": text, "parse_mode": "Markdown"}, "reload")


async def send_stream_verification_message(symbols: list[str]) -> None:
    """Confirmación 18:05 ET — va al chat privado."""
    count   = len(symbols)
    now_str = datetime.now(_ET).strftime("%H:%M:%S ET")
    text = (
        f"✅ *Stream verificado (18:05 ET)*\n"
        f"🎯 *{count}* contratos activos para la sesión nocturna\n"
        f"📌 Subyacentes: `{', '.join(config.WATCH_SYMBOLS)}`\n"
        f"🕐 {now_str}"
    )
    await _post_private({"text": text, "parse_mode": "Markdown"}, "stream verification")


# ── Mensajes de sesión (sync — renew_session.py corre en executor) ────────────

def send_session_renewed(expiration: object, railway_updated: bool) -> None:
    """Notifica renovacion exitosa de sesion — va al chat privado."""
    railway_str = (
        "Railway actualizado"
        if railway_updated
        else "Railway NO actualizado — actualizar manualmente"
    )
    text = (
        f"🔑 *Sesion Tastytrade renovada*\n"
        f"Expira: {expiration}\n"
        f"{railway_str}"
    )
    _post_private_sync({"text": text, "parse_mode": "Markdown"}, "session renewed")


def send_session_renewal_failed(error: str) -> None:
    """Alerta de fallo en renovacion de sesion — va al chat privado."""
    text = (
        f"❌ *Fallo renovacion sesion Tastytrade*\n"
        f"Error: {error}\n\n"
        f"Ejecuta manualmente desde Mac:\n"
        f"`python renew_session.py`\n"
        f"y actualiza TT_SESSION_JSON en Railway."
    )
    _post_private_sync({"text": text, "parse_mode": "Markdown"}, "session renewal failed")


def send_shutdown_message() -> None:
    """Notifica al apagar el sistema — va al chat privado."""
    _post_private_sync(
        {"text": "⛔ *Sistema de alertas detenido*", "parse_mode": "Markdown"},
        "shutdown",
    )


def send_session_json_manual_update(new_b64: str, expiration: object) -> None:
    """Envía el base64 al chat privado en chunks cuando Railway no se actualiza."""
    warning_text = (
        f"⚠️ *Railway NO actualizado — acción requerida*\n"
        f"Sesión renovada en memoria. Expira: {expiration}\n\n"
        f"Copia el valor del siguiente mensaje y pégalo en\n"
        f"Railway → Variables → TT_SESSION_JSON → Redeploy"
    )
    _post_private_sync({"text": warning_text, "parse_mode": "Markdown"}, "session json warning")
    for idx, chunk in enumerate(
        [new_b64[i : i + 3800] for i in range(0, len(new_b64), 3800)], 1
    ):
        _post_private_sync(
            {"text": f"TT_SESSION_JSON:\n\n`{chunk}`", "parse_mode": "Markdown"},
            f"session json chunk {idx}",
        )
