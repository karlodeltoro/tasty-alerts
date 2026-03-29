"""
volume_tracker.py — Rastreo de volumen acumulado en ventanas deslizantes.

Con Tastytrade usamos add_trade() en lugar de update():
  - add_trade(symbol, size): cada Trade event del WebSocket empuja su tamaño
    directamente a las colas (sin calcular delta vs total acumulado).

Esto elimina el problema de caché del REST de Schwab — cada trade es un
evento real del exchange, no un snapshot.
"""
import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta

import config

logger = logging.getLogger(__name__)


@dataclass
class TrackResult:
    symbol: str
    underlying: str
    contract_type: str      # "CALL" o "PUT"
    delta: float
    vol_delta: int          # contratos en este trade
    vol_1min: int           # total acumulado en 60s
    vol_5min: int           # total acumulado en 300s


class VolumeTracker:
    def __init__(self) -> None:
        self._last_total: dict[str, int] = {}
        self._q1: dict[str, deque] = defaultdict(deque)
        self._q5: dict[str, deque] = defaultdict(deque)
        self._meta: dict[str, dict] = {}

    def register(self, symbol: str, underlying: str, contract_type: str, delta: float) -> None:
        """Registra un símbolo con su metadata antes de empezar a rastrear."""
        self._meta[symbol] = {
            "underlying": underlying,
            "contract_type": contract_type,
            "delta": delta,
        }

    def add_trade(self, symbol: str, size: int, price: float = 0.0) -> TrackResult | None:
        """
        Registra un trade individual del WebSocket (event-driven).
        A diferencia de update(), no calcula delta vs total — recibe el tamaño
        real del trade directamente del exchange.
        """
        if symbol not in self._meta:
            return None
        if size <= 0:
            return None

        now = datetime.now()
        event = (now, size, price)
        self._q1[symbol].append(event)
        self._q5[symbol].append(event)

        cutoff1 = now - timedelta(seconds=60)
        cutoff5 = now - timedelta(seconds=300)
        _trim(self._q1[symbol], cutoff1)
        _trim(self._q5[symbol], cutoff5)

        meta = self._meta[symbol]
        return TrackResult(
            symbol=symbol,
            underlying=meta["underlying"],
            contract_type=meta["contract_type"],
            delta=meta["delta"],
            vol_delta=size,
            vol_1min=sum(e[1] for e in self._q1[symbol]),
            vol_5min=sum(e[1] for e in self._q5[symbol]),
        )

    def update(self, symbol: str, total_volume: int) -> TrackResult | None:
        """
        Compatibilidad con el sistema REST (Schwab).
        En tasty-alerts usamos add_trade() en su lugar.
        """
        if symbol not in self._meta:
            return None

        now = datetime.now()
        prev = self._last_total.get(symbol)
        if prev is None:
            self._last_total[symbol] = total_volume
            return None

        vol_delta = max(0, total_volume - prev)
        self._last_total[symbol] = total_volume
        if vol_delta == 0:
            return None

        if vol_delta > config.MAX_VOL_DELTA_PER_CYCLE:
            logger.warning(
                f"[TRACKER] {symbol} vol_delta={vol_delta} > MAX ({config.MAX_VOL_DELTA_PER_CYCLE}) — descartado"
            )
            return None

        event = (now, vol_delta, 0.0)
        self._q1[symbol].append(event)
        self._q5[symbol].append(event)
        _trim(self._q1[symbol], now - timedelta(seconds=60))
        _trim(self._q5[symbol], now - timedelta(seconds=300))

        meta = self._meta[symbol]
        return TrackResult(
            symbol=symbol,
            underlying=meta["underlying"],
            contract_type=meta["contract_type"],
            delta=meta["delta"],
            vol_delta=vol_delta,
            vol_1min=sum(e[1] for e in self._q1[symbol]),
            vol_5min=sum(e[1] for e in self._q5[symbol]),
        )

    def get_vol_1min(self, symbol: str) -> int:
        now = datetime.now()
        cutoff = now - timedelta(seconds=60)
        q = self._q1.get(symbol)
        if not q:
            return 0
        _trim(q, cutoff)
        return sum(e[1] for e in q)

    def get_vol_window(self, symbol: str, seconds: int) -> int:
        now = datetime.now()
        cutoff = now - timedelta(seconds=seconds)
        q = self._q5.get(symbol)
        if not q:
            return 0
        _trim(q, now - timedelta(seconds=300))
        return sum(e[1] for e in q if e[0] >= cutoff)

    def get_tape(self, symbol: str, seconds: int) -> list[tuple]:
        """Retorna los trades individuales dentro de la ventana como (timestamp, size, price)."""
        now = datetime.now()
        cutoff = now - timedelta(seconds=seconds)
        q = self._q5.get(symbol)
        if not q:
            return []
        return [e for e in q if e[0] >= cutoff]

    def update_delta(self, symbol: str, delta: float) -> None:
        if symbol in self._meta:
            self._meta[symbol]['delta'] = delta

    def get_all_meta(self) -> dict[str, dict]:
        return self._meta

    def clear(self) -> None:
        self._last_total.clear()
        self._q1.clear()
        self._q5.clear()
        self._meta.clear()


def _trim(q: deque, cutoff: datetime) -> None:
    while q and q[0][0] < cutoff:
        q.popleft()
