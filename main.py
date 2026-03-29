"""
main.py — Punto de entrada del sistema de alertas (Tastytrade).

Uso:
  python main.py

Producción:
  caffeinate -i python main.py >> alerts.log 2>&1 &
"""
import asyncio
import logging
import signal
import sys
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import telegram_notifier as tg
from tasty_stream import TastyAlertSystem

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


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
    scheduler.start()
    logger.info("Scheduler activo — switch 17:15 ET, verificación 18:05 ET")

    loop = asyncio.get_running_loop()

    def _shutdown(sig_name: str) -> None:
        logger.info(f"Señal {sig_name} recibida. Cerrando...")
        tg.send_shutdown_message()
        scheduler.shutdown(wait=False)
        loop.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig.name)

    logger.info("Iniciando sesión de alertas Tastytrade...")
    await system.run_session()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
