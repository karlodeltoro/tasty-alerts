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
from alert_engine import BlockAccumulatorEngine, BlockPrintEngine, PressureCookerEngine, SweepBurstEngine
from alert_store import store, AlertRecord
from schwab_stream import MacroContext, SchwabMacroStream
from volume_tracker import VolumeTracker

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")

_ROOT_MAP = {"/ES": "/ES", "/NQ": "/NQ", "/GC": "/GC"}


class TastyAlertSystem:
    def __init__(self) -> None:
        self.tracker           = VolumeTracker()
        self.engine            = SweepBurstEngine()
        self.block_engine      = BlockPrintEngine()
        self.block_accum_engine = BlockAccumulatorEngine()
        self.pc_engine         = PressureCookerEngine()
        self._active_symbols: list[str] = []
        self._contract_meta: dict[str, dict] = {}
        self._current_expiry: date | None = None
        self._raw_last_fired: dict[str, datetime] = {}
        # ── Pending trades queue (no-quote / reload guard) ────────
        self._pending_trades: list[tuple] = []  # (sym, size, price, is_ask, timestamp)
        self._reloading: bool = False
        # ── Diagnóstico de flujo ──────────────────────────────────
        self._trade_count: int = 0
        self._quote_count: int = 0
        self._greeks_count: int = 0
        self._trade_count_last: int = 0
        self._quote_count_last: int = 0
        self._greeks_count_last: int = 0
        self._first_trade_logged: bool = False
        self._first_quote_logged: bool = False
        self._first_greeks_logged: bool = False
        self._streamer: DXLinkStreamer | None = None
        # ── Schwab macro context ──────────────────────────────────
        self.macro: MacroContext | None = None
        self._macro_stream: SchwabMacroStream | None = None
        # ── Discard counters (F15) ────────────────────────────────
        self._discarded_no_quote: int = 0
        self._discarded_price_filter: int = 0
        self._discarded_unknown_sym: int = 0
        self._discarded_zero_size: int = 0

    # ─────────────────────────────────────────────────────────────
    # Carga de cadena
    # ─────────────────────────────────────────────────────────────

    def _reset_counters(self) -> None:
        self._trade_count = 0
        self._quote_count = 0
        self._greeks_count = 0
        self._trade_count_last = 0
        self._quote_count_last = 0
        self._greeks_count_last = 0
        self._first_trade_logged = False
        self._first_quote_logged = False
        self._first_greeks_logged = False
        self._discarded_no_quote = 0
        self._discarded_price_filter = 0
        self._discarded_unknown_sym = 0
        self._discarded_zero_size = 0

    async def load_chain(self, session: Session, streamer: DXLinkStreamer) -> list[str]:
        """
        Carga las dos expiraciones más próximas para cada subyacente (F16).
        Usa el DXLinkStreamer para obtener el precio del futuro vía Quote snapshot.
        """
        logger.info("=== load_chain() iniciado ===")
        self.tracker.clear()
        self.engine.reset()
        self.block_engine.reset()
        self.block_accum_engine.reset()
        self.pc_engine.reset()
        self._contract_meta.clear()
        self._pending_trades.clear()
        self._reset_counters()
        all_symbols: list[str] = []

        now_et = datetime.now(_ET)
        today = now_et.date()
        today_expired = now_et.hour >= 17
        logger.info(f"  Hora ET: {now_et.strftime('%H:%M:%S')} | today={today} | today_expired={today_expired}")
        logger.info(f"  WATCH_SYMBOLS: {config.WATCH_SYMBOLS}")

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

            logger.info(f"  Chain OK — option_chains={len(chain.option_chains)}, futures={len(chain.futures)}")

            # ── Subchain del root correcto ────────────────────────
            subchain = next((sc for sc in chain.option_chains if sc.root_symbol == root), None)
            if not subchain:
                available_roots = [sc.root_symbol for sc in chain.option_chains]
                logger.warning(f"No se encontró subchain para root={root}. Disponibles: {available_roots}")
                continue

            all_exps = sorted([e.expiration_date for e in subchain.expirations])
            logger.info(f"  Expiraciones disponibles ({len(all_exps)}): {all_exps[:6]}")

            # ── Collect up to MULTI_EXPIRY_COUNT nearest expiries (F16) ──
            target_exps = []
            for exp in sorted(subchain.expirations, key=lambda e: e.expiration_date):
                if len(target_exps) >= config.MULTI_EXPIRY_COUNT:
                    break
                if exp.expiration_date > today:
                    target_exps.append(exp)
                elif exp.expiration_date == today and not today_expired:
                    target_exps.append(exp)

            if not target_exps:
                logger.warning(f"No hay expiración activa para {watch_sym} (today={today}, expired={today_expired})")
                continue

            logger.info(f"Loading {len(target_exps)} expiries: {[e.expiration_date for e in target_exps]}")
            self._current_expiry = target_exps[0].expiration_date

            # ── Futuro front-month ────────────────────────────────
            futures_sym = None
            for fut in sorted(chain.futures, key=lambda f: f.expiration_date):
                if fut.expiration_date >= today:
                    futures_sym = fut.symbol
                    break
            if not futures_sym:
                logger.warning(f"No se encontró futuro activo para {watch_sym}")
                continue

            logger.info(f"  Futuro front-month: {futures_sym}")

            # ── Precio del futuro vía Quote (F4 — 5s timeout + REST fallback) ──
            F: float | None = None
            try:
                await streamer.subscribe(Quote, [futures_sym])
                fq = await asyncio.wait_for(streamer.get_event(Quote), timeout=5.0)
                bp = float(fq.bid_price or 0)
                ap = float(fq.ask_price or 0)
                logger.info(f"  Quote futuro recibida: bid={bp}, ask={ap}")
                if bp > 0 and ap > 0:
                    F = (bp + ap) / 2.0
            except asyncio.TimeoutError:
                logger.warning(f"  Timeout (5s) obteniendo precio de {futures_sym} — intentando REST fallback...")
                try:
                    fut_obj = next((f for f in chain.futures if f.symbol == futures_sym), None)
                    if fut_obj and hasattr(fut_obj, 'mark'):
                        mark_val = float(fut_obj.mark or 0)
                        if mark_val > 0:
                            F = mark_val
                            logger.info(f"  REST fallback precio {futures_sym}: {F:.2f}")
                        else:
                            logger.warning(f"  REST fallback: mark=0 para {futures_sym}")
                    else:
                        logger.warning(f"  REST fallback: no mark disponible para {futures_sym}")
                except Exception as rest_err:
                    logger.warning(f"  REST fallback error: {rest_err}")
                if F is None:
                    logger.warning(f"  WARN: loading all strikes, no price filter active")
            except Exception as e:
                logger.warning(f"  Error obteniendo precio de {futures_sym}: {e}")

            if F:
                logger.info(f"  Precio {futures_sym}: {F:.2f} | Filtro distancia: ±{config.MAX_STRIKE_DISTANCE_PCT*100:.0f}%")
            else:
                logger.warning(f"  Sin precio para {futures_sym} — registrando TODOS los strikes sin filtro")

            # ── Filtrar strikes y registrar para cada expiry ──────
            total_registered = 0
            for target_exp in target_exps:
                total_strikes = len(target_exp.strikes)
                count = 0
                skipped = 0
                for strike in target_exp.strikes:
                    K = float(strike.strike_price)
                    if F and abs(K - F) / F > config.MAX_STRIKE_DISTANCE_PCT:
                        skipped += 1
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

                logger.info(f"  {watch_sym} {target_exp.expiration_date}: {count} contratos ({skipped} strikes descartados)")
                total_registered += count

            if all_symbols:
                sample = all_symbols[-min(3, len(all_symbols)):]
                logger.info(f"  Muestra de símbolos: {sample}")

        self._active_symbols = all_symbols
        logger.info(f"=== load_chain() completado: {len(all_symbols)} contratos totales ===")
        return all_symbols

    # ─────────────────────────────────────────────────────────────
    # Pending trades helpers
    # ─────────────────────────────────────────────────────────────

    def _enqueue_pending(self, sym: str, size: int, price: float, is_ask: bool) -> None:
        self._pending_trades.append((sym, size, price, is_ask, datetime.now(_ET)))

    def _drain_pending(self) -> None:
        """Process pending trades whose symbol now has quote data; discard stale ones."""
        if not self._pending_trades:
            return
        now = datetime.now(_ET)
        max_age = timedelta(seconds=config.PENDING_TRADE_MAX_AGE_SECONDS)
        remaining = []
        for sym, size, price, is_ask, ts in self._pending_trades:
            if (now - ts) > max_age:
                logger.debug(f"[PENDING] discarding stale trade {sym} size={size} age={(now-ts).total_seconds():.1f}s")
                continue
            if sym not in self._contract_meta:
                remaining.append((sym, size, price, is_ask, ts))
                continue
            meta = self._contract_meta[sym]
            bid = meta.get('bid', 0.0)
            ask = meta.get('ask', 0.0)
            if bid == 0.0 and ask == 0.0:
                remaining.append((sym, size, price, is_ask, ts))
                continue
            # Quote now available — process
            self._process_trade(sym, size, price, is_ask, meta)
        self._pending_trades = remaining

    # ─────────────────────────────────────────────────────────────
    # Core trade processing
    # ─────────────────────────────────────────────────────────────

    def _process_trade(self, sym: str, size: int, price: float, is_ask: bool, meta: dict) -> None:
        """Process a single validated trade through engines."""
        bid  = meta.get('bid', 0.0)
        ask  = meta.get('ask', 0.0)
        mark = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0

        # F14 — Delta-aware price filter
        delta = self.tracker.get_all_meta().get(sym, {}).get('delta', 0.0)
        min_price = config.MIN_CONTRACT_PRICE * (1 - abs(delta))
        min_price = max(min_price, config.MIN_CONTRACT_PRICE_FLOOR)
        if mark < min_price:
            self._discarded_price_filter += 1
            logger.debug(f"[DROP:PRICE] {sym} mark={mark:.2f} < min={min_price:.2f}")
            return

        meta['last_trade_price'] = price
        meta['last_trade_bid']   = bid   # snapshot at trade time — prevents stale-quote mismatch
        meta['last_trade_ask']   = ask
        ask_price = meta.get('ask', 0.0)
        result = self.tracker.add_trade(sym, size, price, is_ask=is_ask, ask_price=ask_price)
        if not result:
            return

        if config.RAW_ALERT_MODE:
            if mark >= config.RAW_MIN_PRICE:
                vol_1min = self.tracker.get_vol_1min(sym)
                if vol_1min >= config.RAW_MIN_VOL_1MIN:
                    last_fired = self._raw_last_fired.get(sym)
                    now = datetime.now(_ET)
                    if last_fired is None or (now - last_fired).total_seconds() >= config.RAW_COOLDOWN_SECONDS:
                        self._raw_last_fired[sym] = now
                        direction = 'CALL' if meta.get('is_call') else 'PUT'
                        if meta.get('expiry_date'):
                            asyncio.create_task(self._send_raw_alert(
                                direction, meta['strike'], meta['expiry_date'], vol_1min, mark,
                                meta.get('underlying', '/ES'),
                            ))
        else:
            trade_delta = self.tracker.get_all_meta().get(sym, {}).get('delta', 0.0)
            ask_ratio   = self.tracker.get_ask_ratio_1min(sym)

            # Block Print (single large fill)
            exec_price = price
            ask_price  = meta.get('ask', 0.0)
            if self.block_engine.check(sym, size, trade_delta, self._is_market_hours(),
                                       exec_price=exec_price, ask_price=ask_price):
                asyncio.create_task(self._send_block_print(sym, size, ask_ratio))

            # Block Accumulator (rolling 30s)
            vol_30s = self.tracker.get_vol_30s(sym)
            if self.block_accum_engine.check(sym, vol_30s, ask_ratio, self._is_market_hours()):
                asyncio.create_task(self._send_block_accum(sym, vol_30s, ask_ratio))

            # Sweep Burst — event-driven (F5)
            vol_snapshot  = {s: self.tracker.get_vol_1min(s) for s in self._active_symbols}
            ask_snapshot  = {s: self.tracker.get_ask_ratio_1min(s) for s in self._active_symbols}
            agg_snapshot  = {s: self.tracker.get_vol_2min_aggressive(s) for s in self._active_symbols}
            burst = self.engine.check(vol_snapshot, self.tracker.get_all_meta(),
                                      ask_snapshot, agg_snapshot)
            if burst:
                direction, group = burst
                asyncio.create_task(self._send_sweep_burst(direction, group))

    # ─────────────────────────────────────────────────────────────
    # Handlers de eventos WebSocket
    # ─────────────────────────────────────────────────────────────

    async def _handle_trades(self, streamer: DXLinkStreamer) -> None:
        async for trade in streamer.listen(Trade):
            self._trade_count += 1

            # Drain pending queue first (may now have quotes)
            self._drain_pending()

            sym = trade.event_symbol
            if not self._first_trade_logged:
                self._first_trade_logged = True
                logger.info(f"[DIAGNÓSTICO] Primer Trade recibido: sym={sym}, size={trade.size}, price={trade.price}")

            if sym not in self._contract_meta:
                self._discarded_unknown_sym += 1
                logger.debug(f"[DROP:UNKNOWN] {sym}")
                continue

            size = trade.size
            if not size or size <= 0:
                self._discarded_zero_size += 1
                logger.debug(f"[DROP:ZERO_SIZE] {sym}")
                continue

            # Guard: if reloading, queue the trade
            if self._reloading:
                self._enqueue_pending(sym, int(size), float(trade.price or 0.0), False)
                continue

            meta = self._contract_meta[sym]
            bid  = meta.get('bid', 0.0)
            ask  = meta.get('ask', 0.0)

            # No quote yet — queue instead of dropping
            if bid == 0.0 and ask == 0.0:
                self._discarded_no_quote += 1
                logger.debug(f"[PENDING] queued trade {sym} size={size} — no quote yet")
                self._enqueue_pending(sym, int(size), float(trade.price or 0.0), False)
                continue

            trade_price = float(trade.price) if trade.price else (bid + ask) / 2.0
            is_ask = ask > 0 and trade_price >= ask
            self._process_trade(sym, int(size), trade_price, is_ask, meta)

    async def _handle_quotes(self, streamer: DXLinkStreamer) -> None:
        async for quote in streamer.listen(Quote):
            self._quote_count += 1
            sym = quote.event_symbol
            if not self._first_quote_logged:
                self._first_quote_logged = True
                logger.info(f"[DIAGNÓSTICO] Primer Quote recibido: sym={sym}, bid={quote.bid_price}, ask={quote.ask_price}")
            if sym in self._contract_meta:
                self._contract_meta[sym]['bid'] = float(quote.bid_price or 0.0)
                self._contract_meta[sym]['ask'] = float(quote.ask_price or 0.0)

    async def _handle_greeks(self, streamer: DXLinkStreamer) -> None:
        async for greeks in streamer.listen(Greeks):
            self._greeks_count += 1
            sym = greeks.event_symbol
            if not self._first_greeks_logged:
                self._first_greeks_logged = True
                logger.info(f"[DIAGNÓSTICO] Primer Greeks recibido: sym={sym}, delta={greeks.delta}, iv={greeks.volatility}")
            if sym in self._contract_meta:
                self.tracker.update_delta(sym, float(greeks.delta or 0.0))
                self._contract_meta[sym]['iv'] = float(greeks.volatility or 0.0)

    async def _periodic_checks(self) -> None:
        _heartbeat_tick = 0
        while True:
            await asyncio.sleep(15)
            _heartbeat_tick += 1

            # Log de flujo cada ~60s (4 ticks × 15s)
            if _heartbeat_tick % 4 == 0:
                trades_delta  = self._trade_count  - self._trade_count_last
                quotes_delta  = self._quote_count  - self._quote_count_last
                greeks_delta  = self._greeks_count - self._greeks_count_last
                self._trade_count_last  = self._trade_count
                self._quote_count_last  = self._quote_count
                self._greeks_count_last = self._greeks_count
                logger.info(
                    f"[HEARTBEAT] contratos={len(self._active_symbols)} | "
                    f"trades_total={self._trade_count} (+{trades_delta}/min) | "
                    f"quotes_total={self._quote_count} (+{quotes_delta}/min) | "
                    f"greeks_total={self._greeks_count} (+{greeks_delta}/min) | "
                    f"discarded: no_quote={self._discarded_no_quote} "
                    f"price={self._discarded_price_filter} "
                    f"unknown={self._discarded_unknown_sym}"
                )
                if self._trade_count == 0 and len(self._active_symbols) > 0:
                    logger.warning("[HEARTBEAT] ALERTA: 0 trades recibidos con contratos suscritos — posible problema de suscripción")

            if config.RAW_ALERT_MODE:
                continue

            # Pressure Cooker — 2min and 5min (F7)
            tracker_meta = self.tracker.get_all_meta()
            for sym in self._active_symbols:
                delta    = tracker_meta.get(sym, {}).get('delta', 0.0)
                vol_2min = self.tracker.get_vol_window(sym, 120)
                vol_5min = self.tracker.get_vol_window(sym, 300)

                fire_2 = self.pc_engine.check_2min(sym, vol_2min, delta)
                if fire_2:
                    tape = self.tracker.get_tape(sym, 120)
                    asyncio.create_task(self._send_pressure_cooker(sym, vol_2min, 2, tape))

                fire_5 = self.pc_engine.check_5min(sym, vol_5min, delta)
                if fire_5:
                    tape = self.tracker.get_tape(sym, 300)
                    asyncio.create_task(self._send_pressure_cooker(sym, vol_5min, 5, tape))

    # ─────────────────────────────────────────────────────────────
    # Envío de alertas
    # ─────────────────────────────────────────────────────────────

    def _get_macro(self) -> str:
        """Returns macro context string for alert enrichment, or empty string."""
        if self._macro_stream is None:
            return ""
        ctx = self._macro_stream.get_context()
        return ctx.format_for_alert()

    def _is_market_hours(self) -> bool:
        from datetime import time as dtime
        t = datetime.now(_ET).time()
        return dtime(9, 1) <= t <= dtime(17, 59)

    async def _send_raw_alert(self, direction, strike, expiry, vol_1min, mark, underlying: str = "/ES"):
        await tg.send_raw_alert(direction, strike, expiry, vol_1min, mark, underlying=underlying)

    async def _send_block_print(self, symbol: str, vol_delta: int, ask_ratio: float = 0.0) -> None:
        meta       = self._contract_meta.get(symbol, {})
        direction  = 'CALL' if meta.get('is_call') else 'PUT'
        bid        = meta.get('last_trade_bid', meta.get('bid', 0.0))
        ask        = meta.get('last_trade_ask', meta.get('ask', 0.0))
        mark       = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0
        exec_price = meta.get('last_trade_price', mark)
        delta      = self.tracker.get_all_meta().get(symbol, {}).get('delta', 0.0)
        expiry     = meta.get('expiry_date', date.today())
        await tg.send_block_print(
            direction=direction, strike=meta.get('strike', 0.0),
            expiry_date=expiry,
            bid=bid, ask=ask, exec_price=exec_price, delta=delta,
            iv=meta.get('iv', 0.0), vol_delta=vol_delta,
            ask_ratio=ask_ratio,
            underlying=meta.get('underlying', '/ES'),
            macro_context=self._get_macro(),
        )
        store.push(AlertRecord(
            alert_type="BLOCK_PRINT",
            direction=direction,
            underlying=meta.get('underlying', '/ES'),
            strike=meta.get('strike', 0.0),
            expiry_date=str(expiry),
            vol=vol_delta,
            ask_ratio=ask_ratio,
            delta=delta,
            iv=meta.get('iv', 0.0),
            bid=bid,
            ask=ask,
            macro_context=self._get_macro(),
            dte=(expiry - date.today()).days if expiry else 0,
        ))

    async def _send_block_accum(self, symbol: str, vol_30s: int, ask_ratio: float) -> None:
        meta      = self._contract_meta.get(symbol, {})
        direction = 'CALL' if meta.get('is_call') else 'PUT'
        bid, ask  = meta.get('bid', 0.0), meta.get('ask', 0.0)
        delta     = self.tracker.get_all_meta().get(symbol, {}).get('delta', 0.0)
        expiry    = meta.get('expiry_date', date.today())
        await tg.send_block_accum(
            direction=direction, strike=meta.get('strike', 0.0),
            expiry_date=expiry,
            bid=bid, ask=ask, delta=delta,
            iv=meta.get('iv', 0.0), vol_30s=vol_30s, ask_ratio=ask_ratio,
            underlying=meta.get('underlying', '/ES'),
            macro_context=self._get_macro(),
        )
        store.push(AlertRecord(
            alert_type="BLOCK_ACCUM",
            direction=direction,
            underlying=meta.get('underlying', '/ES'),
            strike=meta.get('strike', 0.0),
            expiry_date=str(expiry),
            vol=vol_30s,
            ask_ratio=ask_ratio,
            delta=delta,
            iv=meta.get('iv', 0.0),
            bid=bid,
            ask=ask,
            macro_context=self._get_macro(),
            dte=(expiry - date.today()).days if expiry else 0,
        ))

    async def _send_sweep_burst(self, direction: str, group: list[tuple]) -> None:
        tracker_meta = self.tracker.get_all_meta()
        contracts, expiry_date = [], None
        for item in group:
            # item is (sym, vol_1min, ask_ratio)
            sym = item[0]
            vol_1min = item[1]
            ask_ratio = item[2] if len(item) > 2 else 0.0
            meta = self._contract_meta.get(sym, {})
            bid = meta.get('last_trade_bid', meta.get('bid', 0.0))
            ask = meta.get('last_trade_ask', meta.get('ask', 0.0))
            last_price = meta.get('last_trade_price', (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0)
            contracts.append({
                'strike':      meta.get('strike', 0),
                'underlying':  meta.get('underlying', '/ES'),
                'vol_1min':    vol_1min,
                'ask_ratio':   ask_ratio,
                'delta':       tracker_meta.get(sym, {}).get('delta', 0.0),
                'bid':         bid,
                'ask':         ask,
                'last_price':  last_price,
                'expiry_date': meta.get('expiry_date'),
            })
            if expiry_date is None:
                expiry_date = meta.get('expiry_date')
        sweep_underlying = contracts[0].get('underlying', '/ES') if contracts else '/ES'
        await tg.send_sweep_burst(
            direction=direction, contracts=contracts,
            expiry_date=expiry_date or date.today(),
            underlying=sweep_underlying,
            macro_context=self._get_macro(),
        )
        if contracts:
            top = contracts[0]
            exp = expiry_date or date.today()
            store.push(AlertRecord(
                alert_type="SWEEP_BURST",
                direction=direction,
                underlying=top.get('underlying', '/ES'),
                strike=top.get('strike', 0.0),
                expiry_date=str(exp),
                vol=sum(c.get('vol_1min', 0) for c in contracts),
                ask_ratio=top.get('ask_ratio', 0.0),
                delta=top.get('delta', 0.0),
                iv=top.get('iv', 0.0),
                bid=top.get('bid', 0.0),
                ask=top.get('ask', 0.0),
                macro_context=self._get_macro(),
                dte=(exp - date.today()).days,
            ))

    async def _send_pressure_cooker(self, symbol: str, vol_accumulated: int, minutes: int, tape: list) -> None:
        meta      = self._contract_meta.get(symbol, {})
        direction = 'CALL' if meta.get('is_call') else 'PUT'
        bid, ask  = meta.get('bid', 0.0), meta.get('ask', 0.0)
        mark      = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0
        delta     = self.tracker.get_all_meta().get(symbol, {}).get('delta', 0.0)
        expiry    = meta.get('expiry_date', date.today())
        await tg.send_pressure_cooker(
            minutes=minutes, direction=direction, strike=meta.get('strike', 0.0),
            expiry_date=expiry,
            bid=bid, ask=ask, last_price=mark, delta=delta,
            iv=meta.get('iv', 0.0), vol_accumulated=vol_accumulated,
            tape=tape,
            underlying=meta.get('underlying', '/ES'),
            macro_context=self._get_macro(),
        )
        store.push(AlertRecord(
            alert_type="PRESSURE_COOKER",
            direction=direction,
            underlying=meta.get('underlying', '/ES'),
            strike=meta.get('strike', 0.0),
            expiry_date=str(expiry),
            vol=vol_accumulated,
            ask_ratio=0.0,
            delta=delta,
            iv=meta.get('iv', 0.0),
            bid=bid,
            ask=ask,
            macro_context=self._get_macro(),
            dte=(expiry - date.today()).days if expiry else 0,
        ))

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
                logger.warning(
                    f"TT_SESSION_JSON expirado ({session.session_expiration}) "
                    "— intentando auto-renovación con remember_token..."
                )
                import renew_session as _rs
                if _rs.renew():
                    new_b64 = os.getenv("TT_SESSION_JSON")
                    new_session = Session.deserialize(base64.b64decode(new_b64).decode())
                    if _check_expiry(new_session):
                        logger.info(f"Auto-renovación exitosa (expira {new_session.session_expiration})")
                        return new_session
                logger.warning("Auto-renovación fallida, intentando session.json...")
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

        # ── 3. Intentar remember_token del JSON expirado antes de password ──
        import json as _json
        remember_token = None
        if os.getenv("TT_SESSION_JSON"):
            try:
                data = _json.loads(base64.b64decode(os.getenv("TT_SESSION_JSON")).decode())
                remember_token = data.get("remember_token")
            except Exception:
                pass
        if not remember_token and os.path.exists(session_file):
            try:
                data = _json.loads(open(session_file).read())
                remember_token = data.get("remember_token")
            except Exception:
                pass

        if remember_token:
            try:
                logger.info("Intentando renovar sesión expirada con remember_token...")
                session = Session(login=config.TT_USERNAME, remember_token=remember_token, remember_me=True)
                with open(session_file, "w") as f:
                    f.write(session.serialize())
                new_b64 = base64.b64encode(session.serialize().encode()).decode()
                os.environ["TT_SESSION_JSON"] = new_b64
                logger.info(f"Sesión renovada con remember_token. Expira: {session.session_expiration}")
                return session
            except Exception as e:
                logger.warning(f"remember_token renewal fallido en _make_session: {e}")

        # ── 4. Password login — último recurso, requiere dispositivo autorizado ──
        logger.info("Intentando login con username/password (requiere dispositivo autorizado)...")
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
            self._streamer = streamer
            symbols = await self.load_chain(session, streamer)
            if not symbols:
                logger.error("No se cargaron contratos — abortando run_session().")
                return

            await tg.send_startup_message(len(symbols), self._current_expiry)

            # Phase 2 — Start Schwab macro context stream if enabled
            if config.SCHWAB_ENABLED and self._macro_stream is None:
                self._macro_stream = SchwabMacroStream()
                await self._macro_stream.start()
                logger.info("[SCHWAB] MacroContext stream started")

            # F1+F2 — Subscription order: Quote first, warmup, then Greeks, Trade last
            logger.info(f"Registrando suscripción Quote para {len(symbols)} símbolos...")
            await streamer.subscribe(Quote, symbols)
            logger.info(f"  Quote OK.")

            logger.info(f"Esperando {config.QUOTE_WARMUP_SECONDS}s para warmup de quotes...")
            await asyncio.sleep(config.QUOTE_WARMUP_SECONDS)

            logger.info(f"Registrando suscripción Greeks para {len(symbols)} símbolos...")
            await streamer.subscribe(Greeks, symbols)
            logger.info(f"  Greeks OK.")

            logger.info(f"Registrando suscripción Trade para {len(symbols)} símbolos...")
            await streamer.subscribe(Trade, symbols)
            logger.info(f"  Trade OK. Muestra: {symbols[:3]}")

            logger.info(f"=== Streaming activo — {len(symbols)} contratos suscritos. Esperando eventos... ===")

            await asyncio.gather(
                self._handle_trades(streamer),
                self._handle_quotes(streamer),
                self._handle_greeks(streamer),
                self._periodic_checks(),
            )
            self._streamer = None

    async def reload_chain(self) -> None:
        """
        Recarga la cadena de opciones y re-suscribe en el streamer principal activo.
        IMPORTANTE: reutiliza self._streamer para no romper el flujo de eventos.
        F9 — safe reload with pending queue guard.
        """
        logger.info("=== reload_chain() iniciado ===")
        if self._streamer is None:
            logger.warning("reload_chain() llamado pero no hay streamer activo — ignorando")
            return

        # F9 — set reload guard before load_chain
        self._reloading = True
        try:
            loop = asyncio.get_running_loop()
            session = await loop.run_in_executor(None, self._make_session)

            new_symbols = await self.load_chain(session, self._streamer)
            if not new_symbols:
                logger.error("reload_chain(): load_chain() devolvió 0 contratos")
                await tg.send_reload_message(0, self._current_expiry, underlying=", ".join(config.WATCH_SYMBOLS))
                return

            logger.info(f"Re-suscribiendo {len(new_symbols)} contratos en streamer principal...")
            # F1+F2 — same subscription order on reload
            await self._streamer.subscribe(Quote,  new_symbols)
            await asyncio.sleep(config.QUOTE_WARMUP_SECONDS)
            await self._streamer.subscribe(Greeks, new_symbols)
            await self._streamer.subscribe(Trade,  new_symbols)
            logger.info(f"  Suscripciones Trade/Quote/Greeks registradas. Muestra: {new_symbols[:3]}")

        finally:
            # F9 — always clear flag, then drain
            self._reloading = False
            self._drain_pending()

        await tg.send_reload_message(len(new_symbols), self._current_expiry, underlying=", ".join(config.WATCH_SYMBOLS))
        logger.info(f"=== reload_chain() completado: {len(new_symbols)} contratos activos ===")

    async def verify_stream(self) -> None:
        if self._active_symbols:
            logger.info(f"Verificación 18:05 OK — {len(self._active_symbols)} contratos")
            await tg.send_stream_verification_message(self._active_symbols)
        else:
            logger.warning("Sin contratos activos, forzando recarga...")
            await self.reload_chain()
