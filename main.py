"""
main.py — Punto de entrada del sistema de alertas (Tastytrade).

Uso:
  python main.py

Producción:
  caffeinate -i python main.py >> alerts.log 2>&1 &
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

    scheduler = AsyncIOScheduler(timezone=ET)
    scheduler.add_job(
        system.reload_chain,
        trigger="cron",
        hour=17,
        minute=15,
        id="switch_expiration",
    )
    scheduler.add_job(
        system.verify_stream,
        trigger="cron",
        hour=18,
        minute=5,
        id="verify_stream",
    )
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
    scheduler.start()
    logger.info("Scheduler activo — switch 17:15 ET, verificación 18:05 ET, renovación sesión cada 8h, watchdog expiración cada 1h")

    loop = asyncio.get_running_loop()
    _stop = asyncio.Event()

    def _shutdown(sig_name: str) -> None:
        logger.info(f"Señal {sig_name} recibida. Cerrando...")
        tg.send_shutdown_message()
        scheduler.shutdown(wait=False)
        _stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig.name)

    logger.info("Iniciando sesión de alertas Tastytrade...")
    retry_delay = 30
    _device_challenge_retries = 0
    while not _stop.is_set():
        try:
            await system.run_session()
            # run_session() terminó sin excepción (no debería ocurrir)
            logger.warning("run_session() terminó sin error inesperadamente.")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Sesión terminada con error: {e}")
            if "device_challenge" in str(e).lower():
                _msg = (
                    "🔐 *Tastytrade requiere login manual*\n\n"
                    "El remember_token expiró y el password login requiere OTP.\n\n"
                    "*Acción requerida:*\n"
                    "`python login.py` desde Mac\n"
                    "Luego actualizar TT_SESSION_JSON en Railway."
                )
                tg.send_message(_msg)
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
    except (KeyboardInterrupt, SystemExit):
        pass
