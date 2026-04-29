"""
alert_engine.py — Motores de detección de patrones institucionales.

Sweep Burst: ≥N contratos de la misma dirección acumulan ≥V vol total en 60s.
Block Print: una sola transacción ≥ umbral (150 mercado / 100 after-hours).
Block Accumulator: vol acumulado en 30s ≥ umbral (300 contratos).
Pressure Cooker: acumulación sostenida ≥500 contratos en ventana de 2min o 5min.

Ningún engine usa delta como condición de disparo. El delta se muestra en las
alertas de Telegram pero no bloquea ningún trigger.
Cooldowns independientes por dirección (Sweep) o por símbolo (Block/PC).
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")


def _now() -> datetime:
    """Timezone-aware now in ET (F11)."""
    return datetime.now(_ET)


class SweepBurstEngine:
    def __init__(self) -> None:
        # direction ("CALL" o "PUT") → último datetime de alerta
        self._last_fired: dict[str, datetime] = {}

    def check(
        self,
        vol_snapshot: dict[str, int],      # symbol → vol total acumulado en 60s
        tracker_meta: dict[str, dict],     # symbol → {underlying, contract_type, delta}
        ask_ratio_snapshot: dict[str, float] | None = None,  # symbol → ask_ratio (F10)
        aggressive_2min_snapshot: dict[str, int] | None = None,  # symbol → aggressive vol 2min
    ) -> tuple[str, list[tuple[str, int, float]]] | None:
        """
        Evalúa si se cumple la condición de Sweep Burst.
        Retorna (direction, [(symbol, vol, ask_ratio), ...]) ordenado por vol desc, o None.

        Condición: ≥ SWEEP_BURST_MIN_CONTRACTS_B contratos con aggressive_2min_vol ≥ SWEEP_BURST_MIN_VOL_B
                   Falls back to vol_snapshot if aggressive_2min_snapshot is None.
        """
        ask_ratios = ask_ratio_snapshot or {}
        agg = aggressive_2min_snapshot or vol_snapshot

        calls: list[tuple[str, int, float]] = []
        puts:  list[tuple[str, int, float]] = []

        for sym, vol in agg.items():
            meta = tracker_meta.get(sym)
            if not meta:
                continue
            ct = meta.get('contract_type')
            ratio = ask_ratios.get(sym, 0.0)
            if ct == 'CALL':
                if vol >= config.SWEEP_BURST_MIN_VOL_B:
                    calls.append((sym, vol, ratio))
            elif ct == 'PUT':
                if vol >= config.SWEEP_BURST_MIN_VOL_B:
                    puts.append((sym, vol, ratio))

        for direction, group in [('CALL', calls), ('PUT', puts)]:
            if len(group) >= config.SWEEP_BURST_MIN_CONTRACTS_B and self._can_fire(direction):
                self._last_fired[direction] = _now()
                group.sort(key=lambda x: x[1], reverse=True)
                logger.info(
                    f"SWEEP BURST {direction} — {len(group)} contratos dispararon"
                )
                return direction, group

        return None

    def _can_fire(self, direction: str) -> bool:
        last = self._last_fired.get(direction)
        if last is None:
            return True
        return (_now() - last) >= timedelta(seconds=config.ALERT_COOLDOWN_SECONDS)

    def reset(self) -> None:
        """Limpia el historial al inicio de cada sesión."""
        self._last_fired.clear()


class PressureCookerEngine:
    """
    Detecta acumulación sostenida en un único contrato:
      - ventana 2min: vol ≥ PRESSURE_COOKER_2MIN_VOL (default 250)
      - ventana 5min: vol ≥ PRESSURE_COOKER_5MIN_VOL (default 500)

    Umbral incremental: tras disparar, solo re-dispara cuando el vol supera el
    último vol registrado + umbral, además del cooldown.
    Evita spam del mismo nivel de acumulación.

    Cooldown independiente por símbolo, separado para 2min vs 5min.
    """

    def __init__(self) -> None:
        self._last_fired_2min:     dict[str, datetime] = {}
        self._last_fired_5min:     dict[str, datetime] = {}
        self._last_fired_vol_2min: dict[str, int]      = {}
        self._last_fired_vol_5min: dict[str, int]      = {}

    def check(
        self, symbol: str, vol_5min: int, delta: float
    ) -> bool:
        """
        Legacy interface — runs only the 5min check.
        Retorna fire_5min.
        """
        return self.check_5min(symbol, vol_5min, delta)

    def check_2min(self, symbol: str, vol_2min: int, delta: float) -> bool:
        """
        2-minute pressure check.
        Retorna True si dispara.
        """
        now = _now()
        next_2 = self._last_fired_vol_2min.get(symbol, 0) + config.PRESSURE_COOKER_2MIN_VOL
        fire_2 = (
            vol_2min >= config.PRESSURE_COOKER_2MIN_VOL
            and vol_2min >= next_2
            and self._can_fire(symbol, self._last_fired_2min)
        )
        if fire_2:
            self._last_fired_2min[symbol]     = now
            self._last_fired_vol_2min[symbol] = vol_2min
            logger.info(
                f"PRESSURE COOKER 2min {symbol} — {vol_2min} contratos en 2min "
                f"(next ≥ {vol_2min + config.PRESSURE_COOKER_2MIN_VOL})"
            )
        return fire_2

    def check_5min(self, symbol: str, vol_5min: int, delta: float) -> bool:
        """
        5-minute pressure check.
        Retorna True si dispara.
        """
        now = _now()
        next_5 = self._last_fired_vol_5min.get(symbol, 0) + config.PRESSURE_COOKER_5MIN_VOL
        fire_5 = (
            vol_5min >= config.PRESSURE_COOKER_5MIN_VOL
            and vol_5min >= next_5
            and self._can_fire(symbol, self._last_fired_5min)
        )
        if fire_5:
            self._last_fired_5min[symbol]     = now
            self._last_fired_vol_5min[symbol] = vol_5min
            logger.info(
                f"PRESSURE COOKER 5min {symbol} — {vol_5min} contratos en 5min "
                f"(next ≥ {vol_5min + config.PRESSURE_COOKER_5MIN_VOL})"
            )
        return fire_5

    def _can_fire(self, symbol: str, d: dict[str, datetime]) -> bool:
        last = d.get(symbol)
        if last is None:
            return True
        return (_now() - last) >= timedelta(
            seconds=config.PRESSURE_COOKER_COOLDOWN_SECONDS
        )

    def reset(self) -> None:
        """Limpia el historial al inicio de cada sesión."""
        self._last_fired_2min.clear()
        self._last_fired_5min.clear()
        self._last_fired_vol_2min.clear()
        self._last_fired_vol_5min.clear()


class BlockPrintEngine:
    """
    Detecta transacciones individuales grandes en el T&S.
    Una orden = un disparo. No acumula volumen.

    Horario dual (America/New_York):
      - Mercado (9:01–17:59): trade_size ≥ BLOCK_PRINT_MARKET_MIN_VOL (150)
      - Fuera de mercado (18:00–9:00): trade_size ≥ BLOCK_PRINT_AFTER_HOURS_MIN_VOL (100)
    Cooldown: BLOCK_PRINT_COOLDOWN_SECONDS por símbolo.
    """

    def __init__(self) -> None:
        self._last_fired: dict[str, datetime] = {}

    def check(
        self, symbol: str, trade_size: int, delta: float,
        is_market_hours: bool, exec_price: float = 0.0, ask_price: float = 0.0
    ) -> bool:
        if is_market_hours:
            if trade_size >= config.BLOCK_PRINT_MARKET_MIN_VOL:
                # 200+ contracts — fire regardless of execution price
                pass
            elif trade_size >= config.BLOCK_PRINT_MEDIUM_MIN_VOL:
                # 150-199 contracts — require price >= ask (aggressive fill)
                if exec_price < ask_price:
                    logger.debug(f"[DROP:BLOCK] {symbol} size={trade_size} exec={exec_price:.2f} < ask={ask_price:.2f}")
                    return False
            elif trade_size >= config.BLOCK_PRINT_SMALL_MIN_VOL:
                # 100-149 contracts — require price strictly above ask
                if exec_price <= ask_price:
                    logger.debug(f"[DROP:BLOCK] {symbol} size={trade_size} exec={exec_price:.2f} <= ask={ask_price:.2f}")
                    return False
            else:
                # Below 100 contracts during market hours — never fire
                return False
        else:
            if trade_size < config.BLOCK_PRINT_AFTER_HOURS_MIN_VOL:
                return False

        if not self._can_fire(symbol):
            return False

        self._last_fired[symbol] = _now()
        logger.info(f"BLOCK PRINT {symbol} — trade_size={trade_size} |delta|={abs(delta):.2f} market_hours={is_market_hours}")
        return True

    def _can_fire(self, symbol: str) -> bool:
        last = self._last_fired.get(symbol)
        if last is None:
            return True
        return (_now() - last) >= timedelta(
            seconds=config.BLOCK_PRINT_COOLDOWN_SECONDS
        )

    def reset(self) -> None:
        """Limpia el historial al inicio de cada sesión."""
        self._last_fired.clear()


class BlockAccumulatorEngine:
    """
    Detecta acumulación rápida en un único contrato: vol_30s ≥ BLOCK_ACCUM_MIN_VOL.

    Re-dispara solo cuando vol supera last_fired_vol + BLOCK_ACCUM_MIN_VOL.
    Cooldown: BLOCK_PRINT_COOLDOWN_SECONDS por símbolo.
    """

    def __init__(self) -> None:
        self._last_fired:     dict[str, datetime] = {}
        self._last_fired_vol: dict[str, int]      = {}

    def check(
        self,
        symbol: str,
        vol_30s: int,
        ask_ratio: float,
        is_market_hours: bool,
    ) -> bool:
        """
        Retorna True si dispara el acumulador de bloque.
        """
        if vol_30s < config.BLOCK_ACCUM_MIN_VOL:
            return False

        next_threshold = self._last_fired_vol.get(symbol, 0) + config.BLOCK_ACCUM_MIN_VOL
        if vol_30s < next_threshold:
            return False

        if not self._can_fire(symbol):
            return False

        self._last_fired[symbol]     = _now()
        self._last_fired_vol[symbol] = vol_30s
        logger.info(
            f"BLOCK ACCUM {symbol} — vol_30s={vol_30s} ask_ratio={ask_ratio:.2f} "
            f"market_hours={is_market_hours}"
        )
        return True

    def _can_fire(self, symbol: str) -> bool:
        last = self._last_fired.get(symbol)
        if last is None:
            return True
        return (_now() - last) >= timedelta(
            seconds=config.BLOCK_PRINT_COOLDOWN_SECONDS
        )

    def reset(self) -> None:
        """Limpia el historial al inicio de cada sesión."""
        self._last_fired.clear()
        self._last_fired_vol.clear()
