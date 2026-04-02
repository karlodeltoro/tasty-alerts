"""
alert_engine.py — SweepBurstEngine: detecta ráfagas coordinadas en /ES.

Un Sweep Burst se dispara cuando ≥N contratos distintos de la MISMA dirección
(todos CALL o todos PUT) acumulan cada uno ≥V contratos de volumen en una
ventana deslizante de 60 segundos, con |delta| ≥ MIN_DELTA.

Cooldown: la misma dirección no puede disparar durante ALERT_COOLDOWN_SECONDS.
"""
import logging
from collections import defaultdict, deque
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)


class SweepBurstEngine:
    def __init__(self) -> None:
        # direction ("CALL" o "PUT") → último datetime de alerta
        self._last_fired: dict[str, datetime] = {}

    def check(
        self,
        vol_snapshot: dict[str, int],   # symbol → vol acumulado en ventana 60s
        tracker_meta: dict[str, dict],  # symbol → {underlying, contract_type, delta}
    ) -> tuple[str, list[tuple[str, int]]] | None:
        """
        Evalúa si se cumple la condición de Sweep Burst.
        Retorna (direction, [(symbol, vol), ...]) ordenado por vol desc, o None.
        """
        # Separar contratos elegibles por dirección para cada condición
        # Condición A: ≥ SWEEP_BURST_MIN_VOL (50)
        # Condición B: ≥ SWEEP_BURST_MIN_VOL_B (75)
        calls_a: list[tuple[str, int]] = []
        puts_a:  list[tuple[str, int]] = []
        calls_b: list[tuple[str, int]] = []
        puts_b:  list[tuple[str, int]] = []

        for sym, vol in vol_snapshot.items():
            meta = tracker_meta.get(sym)
            if not meta:
                continue
            if abs(meta.get('delta', 0.0)) < config.MIN_DELTA:
                continue
            ct = meta.get('contract_type')
            if ct == 'CALL':
                if vol >= config.SWEEP_BURST_MIN_VOL:
                    calls_a.append((sym, vol))
                if vol >= config.SWEEP_BURST_MIN_VOL_B:
                    calls_b.append((sym, vol))
            elif ct == 'PUT':
                if vol >= config.SWEEP_BURST_MIN_VOL:
                    puts_a.append((sym, vol))
                if vol >= config.SWEEP_BURST_MIN_VOL_B:
                    puts_b.append((sym, vol))

        for direction, group_a, group_b in [
            ('CALL', calls_a, calls_b),
            ('PUT',  puts_a,  puts_b),
        ]:
            fires_a = len(group_a) >= config.SWEEP_BURST_MIN_CONTRACTS
            fires_b = len(group_b) >= config.SWEEP_BURST_MIN_CONTRACTS_B
            if (fires_a or fires_b) and self._can_fire(direction):
                # Usar el grupo con más vol total como representación
                group = group_a if fires_a else group_b
                self._last_fired[direction] = datetime.now()
                group.sort(key=lambda x: x[1], reverse=True)
                condition = "A" if fires_a else "B"
                logger.info(
                    f"SWEEP BURST {direction} [cond {condition}] — {len(group)} contratos dispararon"
                )
                return direction, group

        return None

    def _can_fire(self, direction: str) -> bool:
        last = self._last_fired.get(direction)
        if last is None:
            return True
        return (datetime.now() - last) >= timedelta(seconds=config.ALERT_COOLDOWN_SECONDS)

    def reset(self) -> None:
        """Limpia el historial al inicio de cada sesión."""
        self._last_fired.clear()


class PressureCookerEngine:
    """
    Detecta acumulación sostenida en un único contrato:
      - ventana 2min: vol ≥ PRESSURE_COOKER_2MIN_VOL (default 250)
      - ventana 5min: vol ≥ PRESSURE_COOKER_5MIN_VOL (default 500)

    Filtros delta: MIN_DELTA ≤ |delta| ≤ MAX_DELTA (igual que el resto del sistema).

    Umbral incremental: tras disparar, solo re-dispara cuando el vol supera el
    último vol registrado + threshold (250 para 2min, 500 para 5min), además del
    cooldown. Evita spam del mismo nivel de acumulación.

    Cooldown independiente por (símbolo × ventana).
    """

    def __init__(self) -> None:
        self._last_fired_2min:     dict[str, datetime] = {}
        self._last_fired_5min:     dict[str, datetime] = {}
        self._last_fired_vol_2min: dict[str, int]      = {}  # vol en el último disparo 2min
        self._last_fired_vol_5min: dict[str, int]      = {}  # vol en el último disparo 5min

    def check(
        self, symbol: str, vol_2min: int, vol_5min: int, delta: float
    ) -> tuple[bool, bool]:
        """
        Retorna (fire_2min, fire_5min).
        Registra cooldown y umbral de vol en cada ventana que dispara.
        """
        abs_delta = abs(delta)
        if abs_delta < config.MIN_DELTA or abs_delta > config.MAX_DELTA:
            return False, False

        now = datetime.now()

        next_2 = self._last_fired_vol_2min.get(symbol, 0) + config.PRESSURE_COOKER_2MIN_VOL
        fire_2 = (
            vol_2min >= config.PRESSURE_COOKER_2MIN_VOL
            and vol_2min >= next_2
            and self._can_fire(symbol, self._last_fired_2min)
        )

        next_5 = self._last_fired_vol_5min.get(symbol, 0) + config.PRESSURE_COOKER_5MIN_VOL
        fire_5 = (
            vol_5min >= config.PRESSURE_COOKER_5MIN_VOL
            and vol_5min >= next_5
            and self._can_fire(symbol, self._last_fired_5min)
        )

        if fire_2:
            self._last_fired_2min[symbol]     = now
            self._last_fired_vol_2min[symbol] = vol_2min
            logger.info(
                f"PRESSURE COOKER 2min {symbol} — {vol_2min} contratos en 2min "
                f"(next ≥ {vol_2min + config.PRESSURE_COOKER_2MIN_VOL})"
            )
        if fire_5:
            self._last_fired_5min[symbol]     = now
            self._last_fired_vol_5min[symbol] = vol_5min
            logger.info(
                f"PRESSURE COOKER 5min {symbol} — {vol_5min} contratos en 5min "
                f"(next ≥ {vol_5min + config.PRESSURE_COOKER_5MIN_VOL})"
            )
        return fire_2, fire_5

    def _can_fire(self, symbol: str, d: dict[str, datetime]) -> bool:
        last = d.get(symbol)
        if last is None:
            return True
        return (datetime.now() - last) >= timedelta(
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
    Detecta acumulación de bloque por contrato.

    Lógica:
      - Ventana deslizante de 60s (deque) por símbolo: solo cuenta vol_delta
        de los últimos 60 segundos.
      - También mantiene un acumulador total de sesión (_cum_vol) que nunca
        se resetea.
      - Dispara cuando:
          vol_1min >= BLOCK_PRINT_MIN_VOL  (100 contratos en el último minuto)
          AND cum_vol >= last_fired_vol + BLOCK_PRINT_MIN_VOL  (100 nuevos desde la última alerta)
          AND cooldown de BLOCK_PRINT_COOLDOWN_SECONDS cumplido
      - Al disparar, registra el cum_vol actual como umbral base para la
        siguiente alerta: primer fire a 100, siguiente a 200, luego 300, etc.
    """

    def __init__(self) -> None:
        self._last_fired:      dict[str, datetime] = {}
        self._last_fired_vol:  dict[str, int]      = {}  # cum_vol en el último disparo
        self._cum_vol:         dict[str, int]      = {}  # acumulado total de sesión
        self._events:          dict[str, deque]    = defaultdict(deque)  # ventana 1min

    def accumulate(self, symbol: str, vol_delta: int) -> None:
        """Registra nuevo volumen (llamar en cada tick con actividad)."""
        key = symbol.split(':')[0]
        now = datetime.now()
        self._events[key].append((now, vol_delta))
        self._cum_vol[key] = self._cum_vol.get(key, 0) + vol_delta
        # Limpiar eventos fuera de la ventana de 60s
        cutoff = now - timedelta(seconds=60)
        q = self._events[key]
        while q and q[0][0] < cutoff:
            q.popleft()

    def _get_schedule_thresholds(self) -> tuple[int, float]:
        """Retorna (min_vol, min_delta) según horario ET actual."""
        et = datetime.now(ZoneInfo("America/New_York"))
        t = et.time()
        from datetime import time as _time
        market_open  = _time(9,  1)
        market_close = _time(17, 59, 59)
        if market_open <= t <= market_close:
            return config.BLOCK_PRINT_MARKET_MIN_VOL, config.BLOCK_PRINT_MARKET_MIN_DELTA
        return config.BLOCK_PRINT_AFTER_HOURS_MIN_VOL, config.BLOCK_PRINT_AFTER_HOURS_MIN_DELTA

    def check(
        self,
        symbol: str,
        delta: float,
        min_delta: float | None = None,
    ) -> bool:
        """
        Evalúa si se cumplen las condiciones de disparo.
        Retorna True si dispara (no resetea el acumulador, solo avanza el umbral).

        Horario dual (America/New_York):
          - Mercado (9:01–17:59): min_vol=150, min_delta=0.40
          - Fuera de mercado (18:00–9:00): min_vol=100, min_delta=0.30
        """
        sched_min_vol, sched_min_delta = self._get_schedule_thresholds()
        effective_min_delta = min_delta if min_delta is not None else sched_min_delta
        abs_delta = abs(delta)
        if abs_delta < effective_min_delta or abs_delta > config.BLOCK_PRINT_MAX_DELTA:
            return False
        key = symbol.split(':')[0]

        # vol en la ventana de 60s
        now = datetime.now()
        cutoff = now - timedelta(seconds=60)
        q = self._events.get(key)
        vol_1min = sum(v for ts, v in q if ts >= cutoff) if q else 0

        if vol_1min < sched_min_vol:
            return False

        # cum_vol debe superar el último umbral disparado + sched_min_vol
        cum = self._cum_vol.get(key, 0)
        next_threshold = self._last_fired_vol.get(key, 0) + sched_min_vol
        if cum < next_threshold:
            return False

        if not self._can_fire(key):
            logger.debug(f"BLOCK PRINT cooldown activo: {key} (cum={cum}, 1min={vol_1min})")
            return False

        # Dispara — avanza el umbral al cum_vol actual
        self._last_fired[key]     = now
        self._last_fired_vol[key] = cum
        logger.info(
            f"BLOCK PRINT {key} — cum={cum} next_thresh={next_threshold} "
            f"1min={vol_1min}  |delta| {abs(delta):.2f} (threshold {effective_min_delta:.2f})"
        )
        return True

    def get_accumulated(self, symbol: str) -> int:
        """Retorna el vol en la ventana de 60s para el símbolo (para la alerta)."""
        key = symbol.split(':')[0]
        now = datetime.now()
        cutoff = now - timedelta(seconds=60)
        q = self._events.get(key)
        return sum(v for ts, v in q if ts >= cutoff) if q else 0

    def _can_fire(self, key: str) -> bool:
        last = self._last_fired.get(key)
        if last is None:
            return True
        return (datetime.now() - last) >= timedelta(
            seconds=config.BLOCK_PRINT_COOLDOWN_SECONDS
        )

    def reset(self) -> None:
        """Limpia el historial al inicio de cada sesión."""
        self._last_fired.clear()
        self._last_fired_vol.clear()
        self._cum_vol.clear()
        self._events.clear()
