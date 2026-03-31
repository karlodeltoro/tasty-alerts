"""
tasty_stream.py — Núcleo del sistema con datos de Tastytrade (v9.x).

Autenticación: Session(login, password) — síncrono, sin OAuth.
Streaming: DXLinkStreamer — async context manager.
  subscribe() y get_event() son síncronos en v9.
  listen() es async iterator.

Ventaja vs Schwab REST:
  Trade.size = contratos reales del exchange por evento — sin caché, sin polling.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import os

from tastytrade import DXLinkStreamer
from tastytrade.dxfeed import Greeks, Quote, Trade
from tastytrade.instruments import Future, NestedFutureOptionChain
from tastytrade.session import Session

import config
import telegram_notifier as tg
from alert_engine import BlockPrintEngine, PressureCookerEngine, SweepBurstEngine
from volume_tracker import VolumeTracker

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")

_ROOT_MAP = {"/ES": "/ES", "/GC": "/GC"}


class TastyAlertSystem:
    def __init__(self) -> None:
        self.tracker      = VolumeTracker()
        self.engine       = SweepBurstEngine()
        self.block_engine = BlockPrintEngine()
        self.pc_engine    = PressureCookerEngine()
        self._active_symbols: list[str] = []
        self._contract_meta: dict[str, dict] = {}
        self._current_expiry: date | None = None
        self._raw_last_fired: dict[str, datetime] = {}

    # ─────────────────────────────────────────────────────────────
    # Carga de cadena
    # ─────────────────────────────────────────────────────────────

    async def load_chain(self, session: Session, streamer: DXLinkStreamer) -> list[str]:
        """
        Carga la cadena 0DTE más próxima para cada subyacente.
        Usa el DXLinkStreamer para obtener el precio del futuro vía Quote snapshot.
        """
        self.tracker.clear()
        self.engine.reset()
        self.block_engine.reset()
        self.pc_engine.reset()
        self._contract_meta.clear()
        all_symbols: list[str] = []

        now_et = datetime.now(_ET)
        today = now_et.date()
        today_expired = now_et.hour >= 17

        for watch_sym in config.WATCH_SYMBOLS:
            root = _ROOT_MAP.get(watch_sym)
            if not root:
                logger.warning(f"Sin mapeo de root para {watch_sym}, omitiendo")
                continue

            logger.info(f"Cargando cadena {watch_sym}...")
            try:
                chain = await NestedFutureOptionChain.a_get_chain(session, watch_sym)
            except Exception as e:
                logger.error(f"Error cargando cadena {watch_sym}: {e}")
                continue

            # ── Subchain del root correcto ────────────────────────
            subchain = next((sc for sc in chain.option_chains if sc.root_symbol == root), None)
            if not subchain:
                logger.warning(f"No se encontró subchain para root={root}")
                continue

            # ── Expiración más próxima ────────────────────────────
            target_exp = None
            for exp in sorted(subchain.expirations, key=lambda e: e.expiration_date):
                if exp.expiration_date > today:
                    target_exp = exp
                    break
                if exp.expiration_date == today and not today_expired:
                    target_exp = exp
                    break
            if not target_exp:
                logger.warning(f"No hay expiración activa para {watch_sym}")
                continue

            self._current_expiry = target_exp.expiration_date
            logger.info(f"  Expiración activa: {target_exp.expiration_date}")

            # ── Futuro front-month ────────────────────────────────
            futures_sym = None
            for fut in sorted(chain.futures, key=lambda f: f.expiration_date):
                if fut.expiration_date >= today:
                    futures_sym = fut.symbol
                    break
            if not futures_sym:
                logger.warning(f"No se encontró futuro activo para {watch_sym}")
                continue

            # ── Precio del futuro vía Quote (síncrono en v9) ──────
            F: float | None = None
            try:
                await streamer.subscribe(Quote, [futures_sym])
                fq = await asyncio.wait_for(streamer.get_event(Quote), timeout=10.0)
                bp = float(fq.bid_price or 0)
                ap = float(fq.ask_price or 0)
                if bp > 0 and ap > 0:
                    F = (bp + ap) / 2.0
            except asyncio.TimeoutError:
                logger.warning(f"Timeout obteniendo precio de {futures_sym} (mercado cerrado?)")
            except Exception as e:
                logger.warning(f"Error obteniendo precio de {futures_sym}: {e}")

            if not F:
                logger.warning(f"No se pudo obtener precio de {futures_sym} — registrando todos los strikes sin filtro de distancia")

            if F:
                logger.info(f"  {futures_sym} precio: {F:.2f}")

            # ── Filtrar strikes y registrar ───────────────────────
            count = 0
            for strike in target_exp.strikes:
                K = float(strike.strike_price)
                if F and abs(K - F) / F > config.MAX_STRIKE_DISTANCE_PCT:
                    continue

                for is_call, sym in [
                    (True,  strike.call_streamer_symbol),
                    (False, strike.put_streamer_symbol),
                ]:
                    if not sym:
                        continue
                    ct_str = 'CALL' if is_call else 'PUT'
                    self._contract_meta[sym] = {
                        'strike':      K,
                        'is_call':     is_call,
                        'expiry_date': target_exp.expiration_date,
                        'futures_sym': futures_sym,
                        'underlying':  watch_sym,
                        'bid':         0.0,
                        'ask':         0.0,
                        'iv':          0.0,
                    }
                    self.tracker.register(sym, watch_sym, ct_str, 0.0)
                    all_symbols.append(sym)
                    count += 1

            logger.info(f"  {watch_sym}: {count} contratos registrados para {target_exp.expiration_date}")

        self._active_symbols = all_symbols
        logger.info(f"Total streamer symbols: {len(all_symbols)}")
        return all_symbols

    # ─────────────────────────────────────────────────────────────
    # Handlers de eventos WebSocket
    # ─────────────────────────────────────────────────────────────

    async def _handle_trades(self, streamer: DXLinkStreamer) -> None:
        async for trade in streamer.listen(Trade):
            sym = trade.event_symbol
            if sym not in self._contract_meta:
                continue

            size = trade.size
            if not size or size <= 0:
                continue

            meta = self._contract_meta[sym]
            bid  = meta.get('bid', 0.0)
            ask  = meta.get('ask', 0.0)
            mark = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0
            if mark > 0 and mark < config.MIN_CONTRACT_PRICE:
                continue

            trade_price = float(trade.price) if trade.price else mark
            meta['last_trade_price'] = trade_price
            result = self.tracker.add_trade(sym, int(size), trade_price)
            if not result:
                continue

            if config.RAW_ALERT_MODE:
                if mark >= config.RAW_MIN_PRICE:
                    vol_1min = self.tracker.get_vol_1min(sym)
                    if vol_1min >= config.RAW_MIN_VOL_1MIN:
                        last_fired = self._raw_last_fired.get(sym)
                        now = datetime.now()
                        if last_fired is None or (now - last_fired).total_seconds() >= config.RAW_COOLDOWN_SECONDS:
                            self._raw_last_fired[sym] = now
                            direction = 'CALL' if meta.get('is_call') else 'PUT'
                            if meta.get('expiry_date'):
                                asyncio.create_task(self._send_raw_alert(
                                    direction, meta['strike'], meta['expiry_date'], vol_1min, mark
                                ))
            else:
                self.block_engine.accumulate(sym, result.vol_delta)
                delta    = self.tracker.get_all_meta().get(sym, {}).get('delta', 0.0)
                vol_acum = self.block_engine.get_accumulated(sym)
                if self.block_engine.check(sym, delta):
                    asyncio.create_task(self._send_block_print(sym, vol_acum))

    async def _handle_quotes(self, streamer: DXLinkStreamer) -> None:
        async for quote in streamer.listen(Quote):
            sym = quote.event_symbol
            if sym in self._contract_meta:
                self._contract_meta[sym]['bid'] = float(quote.bid_price or 0.0)
                self._contract_meta[sym]['ask'] = float(quote.ask_price or 0.0)

    async def _handle_greeks(self, streamer: DXLinkStreamer) -> None:
        async for greeks in streamer.listen(Greeks):
            sym = greeks.event_symbol
            if sym in self._contract_meta:
                self.tracker.update_delta(sym, float(greeks.delta or 0.0))
                self._contract_meta[sym]['iv'] = float(greeks.volatility or 0.0)

    async def _periodic_checks(self) -> None:
        while True:
            await asyncio.sleep(15)
            if config.RAW_ALERT_MODE:
                continue

            vol_snapshot = {sym: self.tracker.get_vol_1min(sym) for sym in self._active_symbols}
            burst = self.engine.check(vol_snapshot, self.tracker.get_all_meta())
            if burst:
                direction, group = burst
                asyncio.create_task(self._send_sweep_burst(direction, group))

            tracker_meta = self.tracker.get_all_meta()
            for sym in self._active_symbols:
                delta    = tracker_meta.get(sym, {}).get('delta', 0.0)
                vol_2min = self.tracker.get_vol_window(sym, 120)
                vol_5min = self.tracker.get_vol_window(sym, 300)
                fire_2, fire_5 = self.pc_engine.check(sym, vol_2min, vol_5min, delta)
                if fire_2:
                    tape = self.tracker.get_tape(sym, 120)
                    asyncio.create_task(self._send_pressure_cooker(sym, vol_2min, 2, tape))
                if fire_5:
                    tape = self.tracker.get_tape(sym, 300)
                    asyncio.create_task(self._send_pressure_cooker(sym, vol_5min, 5, tape))

    # ─────────────────────────────────────────────────────────────
    # Envío de alertas
    # ─────────────────────────────────────────────────────────────

    async def _send_raw_alert(self, direction, strike, expiry, vol_1min, mark):
        tg.send_raw_alert(direction, strike, expiry, vol_1min, mark)

    async def _send_block_print(self, symbol: str, vol_delta: int) -> None:
        meta       = self._contract_meta.get(symbol, {})
        direction  = 'CALL' if meta.get('is_call') else 'PUT'
        bid, ask   = meta.get('bid', 0.0), meta.get('ask', 0.0)
        mark       = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0
        exec_price = meta.get('last_trade_price', mark)
        delta      = self.tracker.get_all_meta().get(symbol, {}).get('delta', 0.0)
        tg.send_block_print(
            direction=direction, strike=meta.get('strike', 0.0),
            expiry_date=meta.get('expiry_date', date.today()),
            bid=bid, ask=ask, exec_price=exec_price, delta=delta,
            iv=meta.get('iv', 0.0), vol_delta=vol_delta,
        )

    async def _send_sweep_burst(self, direction: str, group: list[tuple[str, int]]) -> None:
        tracker_meta = self.tracker.get_all_meta()
        contracts, expiry_date = [], None
        for sym, vol_1min in group:
            meta = self._contract_meta.get(sym, {})
            bid = meta.get('bid', 0.0)
            ask = meta.get('ask', 0.0)
            last_price = meta.get('last_trade_price', (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0)
            contracts.append({
                'strike':      meta.get('strike', 0),
                'vol_1min':    vol_1min,
                'delta':       tracker_meta.get(sym, {}).get('delta', 0.0),
                'bid':         bid,
                'ask':         ask,
                'last_price':  last_price,
                'expiry_date': meta.get('expiry_date'),
            })
            if expiry_date is None:
                expiry_date = meta.get('expiry_date')
        tg.send_sweep_burst(direction=direction, contracts=contracts, expiry_date=expiry_date or date.today())

    async def _send_pressure_cooker(self, symbol: str, vol_accumulated: int, minutes: int, tape: list) -> None:
        meta      = self._contract_meta.get(symbol, {})
        direction = 'CALL' if meta.get('is_call') else 'PUT'
        bid, ask  = meta.get('bid', 0.0), meta.get('ask', 0.0)
        mark      = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0
        delta     = self.tracker.get_all_meta().get(symbol, {}).get('delta', 0.0)
        tg.send_pressure_cooker(
            minutes=minutes, direction=direction, strike=meta.get('strike', 0.0),
            expiry_date=meta.get('expiry_date', date.today()),
            bid=bid, ask=ask, last_price=mark, delta=delta,
            iv=meta.get('iv', 0.0), vol_accumulated=vol_accumulated,
            tape=tape,
        )

    # ─────────────────────────────────────────────────────────────
    # Sesión principal
    # ─────────────────────────────────────────────────────────────

    def _make_session(self) -> Session:
        """
        Crea y autentica la sesión (síncrono en v9).
        Prioridad:
          1. TT_SESSION_JSON env var (base64) — Railway production
          2. session.json local — dev / fallback
          3. Password login — solo dispositivo conocido (Mac)
        """
        import base64
        from datetime import datetime, timezone

        def _check_expiry(session: Session) -> bool:
            """Devuelve True si la sesión aún es válida."""
            exp = session.session_expiration
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            return datetime.now(timezone.utc) < exp

        # ── 1. TT_SESSION_JSON (Railway) ──────────────────────────
        session_b64 = os.getenv("TT_SESSION_JSON")
        if session_b64:
            try:
                serialized = base64.b64decode(session_b64).decode()
                session = Session.deserialize(serialized)
                if _check_expiry(session):
                    logger.info(f"Sesión cargada desde TT_SESSION_JSON (expira {session.session_expiration})")
                    return session
                logger.warning(f"TT_SESSION_JSON expirado ({session.session_expiration}), intentando session.json...")
            except Exception as e:
                logger.warning(f"Error leyendo TT_SESSION_JSON ({e}), intentando session.json...")

        # ── 2. session.json local ─────────────────────────────────
        session_file = os.path.join(os.path.dirname(__file__), "session.json")
        if os.path.exists(session_file):
            try:
                with open(session_file) as f:
                    serialized = f.read()
                session = Session.deserialize(serialized)
                if _check_expiry(session):
                    logger.info(f"Sesión cargada desde {session_file} (expira {session.session_expiration})")
                    return session
                logger.warning(f"session.json expirado ({session.session_expiration}), reautenticando...")
            except Exception as e:
                logger.warning(f"Error leyendo session.json ({e}), reautenticando...")

        # ── 3. Password login (requiere dispositivo conocido) ──────
        session = Session(
            login=config.TT_USERNAME,
            password=config.TT_PASSWORD,
            remember_me=True,
        )
        try:
            with open(session_file, "w") as f:
                f.write(session.serialize())
            logger.info(f"Sesión nueva serializada en {session_file}")
        except Exception as e:
            logger.warning(f"No se pudo serializar sesión: {e}")
        return session

    async def run_session(self) -> None:
        loop = asyncio.get_running_loop()
        session = await loop.run_in_executor(None, self._make_session)
        logger.info("Sesión Tastytrade autenticada.")

        async with DXLinkStreamer(session) as streamer:
            symbols = await self.load_chain(session, streamer)
            if not symbols:
                logger.error("No se cargaron contratos.")
                return

            tg.send_startup_message(len(symbols), self._current_expiry)

            await streamer.subscribe(Trade,  symbols)
            await streamer.subscribe(Quote,  symbols)
            await streamer.subscribe(Greeks, symbols)
            logger.info(f"Streaming activo — {len(symbols)} contratos.")

            await asyncio.gather(
                self._handle_trades(streamer),
                self._handle_quotes(streamer),
                self._handle_greeks(streamer),
                self._periodic_checks(),
            )

    async def reload_chain(self) -> None:
        logger.info("Recargando cadena para nueva sesión...")
        loop = asyncio.get_running_loop()
        session = await loop.run_in_executor(None, self._make_session)
        async with DXLinkStreamer(session) as streamer:
            new_symbols = await self.load_chain(session, streamer)
        tg.send_reload_message(len(new_symbols), self._current_expiry)
        logger.info("Recarga completada.")

    async def verify_stream(self) -> None:
        if self._active_symbols:
            logger.info(f"Verificación 18:05 OK — {len(self._active_symbols)} contratos")
            tg.send_stream_verification_message(self._active_symbols)
        else:
            logger.warning("Sin contratos activos, forzando recarga...")
            await self.reload_chain()
