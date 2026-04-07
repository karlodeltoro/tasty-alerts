"""
alert_engine.py — SweepBurstEngine: detecta ráfagas coordinadas en /ES.

Un Sweep Burst se dispara cuando ≥N contratos distintos de la MISMA dirección
(todos CALL o todos PUT) acumulan cada uno ≥V contratos de volumen en una
ventana deslizante de 60 segundos, con |delta| ≥ MIN_DELTA.

Cooldown: la misma dirección no puede disparar durante ALERT_COOLDOWN_SECONDS.
"""
import logging
from datetime import datetime, timedelta

import config

logger = logging.getLogger(__name__)


class SweepBurstEngine:
    def __init__(self) -> None:
        # direction ("CALL" o "PUT") → último datetime de alerta
        self._last_fired: dict[str, datetime] = {}

    def check(
        self,
        vol_snapshot: dict[str, int],      # symbol → vol total acumulado en 60s
        tracker_meta: dict[str, dict],     # symbol → {underlying, contract_type, delta}
        ask_vol_snapshot: dict[str, int] | None = None,  # symbol → vol en ask en 60s
    ) -> tuple[str, list[tuple[str, int]]] | None:
        """
        Evalúa si se cumple la condición de Sweep Burst.
        Retorna (direction, [(symbol, vol), ...]) ordenado por vol desc, o None.

        Condición: ≥ SWEEP_BURST_MIN_CONTRACTS_B contratos con ask_vol ≥ SWEEP_BURST_MIN_VOL_B
                   (solo cuenta volumen ejecutado en el ask; requiere ask_vol_snapshot)
        """
        _ask = ask_vol_snapshot or {}

        # Separar contratos elegibles por dirección (vol en ask ≥ SWEEP_BURST_MIN_VOL_B)
        calls: list[tuple[str, int]] = []
        puts:  list[tuple[str, int]] = []

        for sym, vol in vol_snapshot.items():
            meta = tracker_meta.get(sym)
            if not meta:
                continue
            if abs(meta.get('delta', 0.0)) < config.MIN_DELTA:
                continue
            ct = meta.get('contract_type')
            ask_vol = _ask.get(sym, 0)
            if ct == 'CALL':
                if ask_vol >= config.SWEEP_BURST_MIN_VOL_B:
                    calls.append((sym, ask_vol))
            elif ct == 'PUT':
                if ask_vol >= config.SWEEP_BURST_MIN_VOL_B:
                    puts.append((sym, ask_vol))

        for direction, group in [('CALL', calls), ('PUT', puts)]:
            if len(group) >= config.SWEEP_BURST_MIN_CONTRACTS_B and self._can_fire(direction):
                self._last_fired[direction] = datetime.now()
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
        return (datetime.now() - last) >= timedelta(seconds=config.ALERT_COOLDOWN_SECONDS)

    def reset(self) -> None:
        """Limpia el historial al inicio de cada sesión."""
        self._last_fired.clear()


class PressureCookerEngine:
    """
    Detecta acumulación sostenida en un único contrato:
      - ventana 5min: vol ≥ PRESSURE_COOKER_5MIN_VOL (default 500)

    Filtros delta: MIN_DELTA ≤ |delta| ≤ MAX_DELTA (igual que el resto del sistema).

    Umbral incremental: tras disparar, solo re-dispara cuando el vol supera el
    último vol registrado + threshold (500 para 5min), además del cooldown.
    Evita spam del mismo nivel de acumulación.

    Cooldown independiente por símbolo.
    """

    def __init__(self) -> None:
        self._last_fired_5min:     dict[str, datetime] = {}
        self._last_fired_vol_5min: dict[str, int]      = {}  # vol en el último disparo 5min

    def check(
        self, symbol: str, vol_5min: int, delta: float
    ) -> bool:
        """
        Retorna fire_5min.
        Registra cooldown y umbral de vol al disparar.
        """
        abs_delta = abs(delta)
        if abs_delta < config.MIN_DELTA or abs_delta > config.MAX_DELTA:
            return False

        now = datetime.now()

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
        return (datetime.now() - last) >= timedelta(
            seconds=config.PRESSURE_COOKER_COOLDOWN_SECONDS
        )

    def reset(self) -> None:
        """Limpia el historial al inicio de cada sesión."""
        self._last_fired_5min.clear()
        self._last_fired_vol_5min.clear()


class BlockPrintEngine:
    """
    Detecta transacciones individuales grandes en el T&S.
    Una orden = un disparo. No acumula volumen.

    Horario dual (America/New_York):
      - Mercado (9:01–17:59): trade_size ≥ 150, |delta| ≥ 0.40
      - Fuera de mercado (18:00–9:00): trade_size ≥ 100, |delta| ≥ 0.30
    Cooldown: BLOCK_PRINT_COOLDOWN_SECONDS por símbolo.
    """

    def __init__(self) -> None:
        self._last_fired: dict[str, datetime] = {}

    def check(self, symbol: str, trade_size: int, delta: float, is_market_hours: bool) -> bool:
        min_vol   = 150 if is_market_hours else 100
        min_delta = config.BLOCK_PRINT_MIN_DELTA if is_market_hours else 0.30

        abs_delta = abs(delta)
        if abs_delta < min_delta or abs_delta > config.BLOCK_PRINT_MAX_DELTA:
            return False
        if trade_size < min_vol:
            return False
        if not self._can_fire(symbol):
            return False

        self._last_fired[symbol] = datetime.now()
        logger.info(f"BLOCK PRINT {symbol} — trade_size={trade_size} |delta|={abs_delta:.2f} market_hours={is_market_hours}")
        return True

    def _can_fire(self, symbol: str) -> bool:
        last = self._last_fired.get(symbol)
        if last is None:
            return True
        return (datetime.now() - last) >= timedelta(
            seconds=config.BLOCK_PRINT_COOLDOWN_SECONDS
        )

    def reset(self) -> None:
        """Limpia el historial al inicio de cada sesión."""
        self._last_fired.clear()
