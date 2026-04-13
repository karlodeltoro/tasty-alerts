"""
main.py — Punto de entrada del sistema de alertas (Tastytrade).

Uso:
  python main.py

Producción:
  caffeinate -i python main.py >> alerts.log 2>&1 &

Horario de operación:
  Activo:  domingo 18:00 ET → viernes 18:00 ET
  Pausado: viernes 18:00 ET → domingo 18:00 ET
"""
import asyncio
import base64
import json
import logging
import signal
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import telegram_notifier as tg
from tasty_stream import TastyAlertSystem
import renew_session

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def _is_weekend_now() -> bool:
    """True si la hora ET actual cae entre viernes 18:00 y domingo 18:00."""
    now  = datetime.now(ET)
    wd   = now.weekday()           # 0=lun … 4=vie, 5=sáb, 6=dom
    mins = now.hour * 60 + now.minute
    close = 18 * 60                # 18:00
    if wd == 4 and mins >= close:  # viernes después de las 6 PM
        return True
    if wd == 5:                    # sábado completo
        return True
    if wd == 6 and mins < close:   # domingo antes de las 6 PM
        return True
    return False


def _expiry_watchdog() -> None:
    """Renueva la sesión si expira en menos de 10h."""
    import os
    session_b64 = os.getenv("TT_SESSION_JSON", "")
    if not session_b64:
        return
    try:
        data = json.loads(base64.b64decode(session_b64).decode())
        exp_str = (
            data.get("session-expiration")
            or data.get("session_expiration")
            or data.get("expiration")
        )
        if not exp_str:
            logger.warning("expiry_watchdog: no se encontró fecha de expiración en TT_SESSION_JSON")
            return
        exp = datetime.fromisoformat(str(exp_str).replace("Z", "+00:00"))
        remaining = exp - datetime.now(timezone.utc)
        if remaining < timedelta(hours=10):
            logger.warning(f"expiry_watchdog: sesión expira en {remaining} — renovando anticipadamente...")
            renew_session.renew()
        else:
            logger.info(f"expiry_watchdog: sesión OK, expira en {remaining}")
    except Exception as e:
        logger.error(f"expiry_watchdog error: {e}")


async def main() -> None:
    system = TastyAlertSystem()

    loop     = asyncio.get_running_loop()
    _stop    = asyncio.Event()
    _weekend = asyncio.Event()

    # ── Detectar arranque en fin de semana ────────────────────────
    if _is_weekend_now():
        _weekend.set()
        logger.info("Arranque en fin de semana — stream pausado hasta domingo 18:00 ET")
        tg.send_private_message(
            "⏸ *BullCore Capital* — arranque en fin de semana\n"
            "Stream inactivo hasta el domingo 18:00 ET"
        )

    # ── Callbacks de fin de semana (definidos aquí para acceder al closure) ──

    async def _enter_weekend() -> None:
        """Viernes 18:00 ET — pausa el stream hasta el domingo."""
        logger.info("=== Fin de semana: cerrando stream ===")
        tg.send_private_message(
            "⏸ *Sistema pausado* — mercado cerrado\n"
            "Reanuda el *domingo a las 6:00 PM ET*"
        )
        _weekend.set()
        # Cancelar sólo la tarea del stream, no el proceso completo
        for task in asyncio.all_tasks():
            if task.get_name() == "run_session":
                task.cancel()
                break

    async def _exit_weekend() -> None:
        """Domingo 18:00 ET — reanuda el stream."""
        logger.info("=== Reanudando stream (domingo 18:00 ET) ===")
        tg.send_private_message("▶️ *Sistema reanudado* — mercado abierto")
        _weekend.clear()

    # ── Scheduler ────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone=ET)

    # Solo días de semana para no generar ruido en el fin de semana
    scheduler.add_job(
        system.reload_chain,
        trigger="cron",
        day_of_week="mon-fri",
        hour=16,
        minute=0,
        id="switch_expiration",
    )
    scheduler.add_job(
        system.verify_stream,
        trigger="cron",
        day_of_week="mon-fri",
        hour=18,
        minute=5,
        id="verify_stream",
    )

    # Renovación de sesión: corre siempre (necesaria aunque el stream esté pausado)
    scheduler.add_job(
        renew_session.renew,
        trigger="interval",
        hours=8,
        id="renew_session",
    )
    scheduler.add_job(
        _expiry_watchdog,
        trigger="interval",
        hours=1,
        id="expiry_watchdog",
    )

    # Cierre y apertura de fin de semana
    scheduler.add_job(
        _enter_weekend,
        trigger="cron",
        day_of_week="fri",
        hour=18,
        minute=0,
        id="weekend_close",
    )
    scheduler.add_job(
        _exit_weekend,
        trigger="cron",
        day_of_week="sun",
        hour=18,
        minute=0,
        id="weekend_open",
    )

    scheduler.start()
    logger.info(
        "Scheduler activo — reload 16:00 ET (lun-vie), verificación 18:05 ET (lun-vie), "
        "renovación sesión cada 8h, watchdog cada 1h, "
        "cierre viernes 18:00 ET, apertura domingo 18:00 ET"
    )

    # ── Signal handlers ──────────────────────────────────────────
    def _shutdown(sig_name: str) -> None:
        logger.info(f"Señal {sig_name} recibida. Cerrando...")
        tg.send_shutdown_message()
        scheduler.shutdown(wait=False)
        _stop.set()
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig.name)

    # ── Loop principal ───────────────────────────────────────────
    logger.info("Iniciando sistema de alertas Tastytrade...")
    retry_delay = 30
    _device_challenge_retries = 0

    while not _stop.is_set():

        # ── Modo fin de semana: esperar hasta que _weekend se limpie ──
        if _weekend.is_set():
            logger.info("Modo fin de semana — stream pausado. Esperando domingo 18:00 ET...")
            while _weekend.is_set() and not _stop.is_set():
                try:
                    await asyncio.wait_for(_stop.wait(), timeout=300)
                except asyncio.TimeoutError:
                    pass
            if _stop.is_set():
                break
            logger.info("Fin de semana terminado — iniciando reconexión...")
            retry_delay = 30
            continue

        # ── Correr sesión como tarea nombrada ────────────────────
        session_task = asyncio.create_task(system.run_session(), name="run_session")
        try:
            await session_task
            logger.warning("run_session() terminó sin error inesperadamente.")

        except asyncio.CancelledError:
            if _weekend.is_set():
                logger.info("Stream cancelado por inicio de fin de semana.")
                continue   # vuelve al tope → entra en espera de fin de semana
            break          # cancelación por _stop o señal del SO

        except Exception as e:
            logger.error(f"Sesión terminada con error: {e}")

            if _weekend.is_set():
                logger.info("Error durante cierre de fin de semana — entrando en modo pausa.")
                continue

            if "device_challenge" in str(e).lower():
                _msg = (
                    "🔐 *Tastytrade requiere login manual*\n\n"
                    "El remember_token expiró y el password login requiere OTP.\n\n"
                    "*Acción requerida:*\n"
                    "`python login.py` desde Mac\n"
                    "Luego actualizar TT_SESSION_JSON en Railway."
                )
                tg.send_private_message(_msg)
                if _device_challenge_retries >= 1:
                    logger.error("device_challenge_required: segundo intento fallido — deteniendo sistema.")
                    break
                _device_challenge_retries += 1
                logger.info("device_challenge_required — esperando 1h antes de reintentar...")
                try:
                    await asyncio.wait_for(_stop.wait(), timeout=3600)
                except asyncio.TimeoutError:
                    pass
                continue

        if _stop.is_set():
            break

        # ── Reconexión con backoff ────────────────────────────────
        logger.info("Renovando sesión antes de reconectar...")
        try:
            renew_session.renew()
        except Exception as re_err:
            logger.error(f"Fallo al renovar sesión pre-reconexión: {re_err}")

        logger.info(f"Reconectando en {retry_delay}s...")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=retry_delay)
        except asyncio.TimeoutError:
            pass
        retry_delay = min(retry_delay * 2, 300)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit, RuntimeError):
        pass
