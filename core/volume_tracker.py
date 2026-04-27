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
from dataclasses import dataclass, field
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
    vol_30s: int = 0        # total acumulado en 30s
    vol_1min_ask: int = 0   # volumen en 60s ejecutado en el ask (price >= ask)


class VolumeTracker:
    def __init__(self) -> None:
        self._last_total: dict[str, int] = {}
        self._q1:     dict[str, deque] = defaultdict(deque)
        self._q5:     dict[str, deque] = defaultdict(deque)
        self._q30:    dict[str, deque] = defaultdict(deque)  # 30-second window
        self._q1_ask: dict[str, deque] = defaultdict(deque)  # solo trades en el ask
        self._q2_agg: dict[str, deque] = defaultdict(deque)  # 2-min aggressive (at/above ask)
        self._meta:   dict[str, dict]  = {}

    def register(self, symbol: str, underlying: str, contract_type: str, delta: float) -> None:
        """Registra un símbolo con su metadata antes de empezar a rastrear."""
        self._meta[symbol] = {
            "underlying": underlying,
            "contract_type": contract_type,
            "delta": delta,
        }

    def add_trade(
        self, symbol: str, size: int, price: float = 0.0,
        is_ask: bool = False, ask_price: float = 0.0
    ) -> "TrackResult | None":
        """
        Registra un trade individual del WebSocket (event-driven).
        A diferencia de update(), no calcula delta vs total — recibe el tamaño
        real del trade directamente del exchange.
        is_ask=True cuando trade.price >= ask del contrato en ese momento.
        ask_price: current ask for above-ask aggressive detection.
        """
        if symbol not in self._meta:
            return None
        if size <= 0:
            return None

        now = datetime.now()
        event = (now, size, price)
        self._q1[symbol].append(event)
        self._q5[symbol].append(event)
        self._q30[symbol].append(event)
        if is_ask:
            self._q1_ask[symbol].append(event)

        # Aggressive = at ask OR strictly above ask
        is_aggressive = is_ask or (price > 0 and ask_price > 0 and price > ask_price)
        if is_aggressive:
            self._q2_agg[symbol].append(event)

        cutoff1  = now - timedelta(seconds=60)
        cutoff5  = now - timedelta(seconds=300)
        cutoff30 = now - timedelta(seconds=30)
        cutoff2a = now - timedelta(seconds=120)
        _trim(self._q1[symbol],    cutoff1)
        _trim(self._q5[symbol],    cutoff5)
        _trim(self._q30[symbol],   cutoff30)
        _trim(self._q1_ask[symbol], cutoff1)
        _trim(self._q2_agg[symbol], cutoff2a)

        meta = self._meta[symbol]
        return TrackResult(
            symbol=symbol,
            underlying=meta["underlying"],
            contract_type=meta["contract_type"],
            delta=meta["delta"],
            vol_delta=size,
            vol_1min=sum(e[1] for e in self._q1[symbol]),
            vol_5min=sum(e[1] for e in self._q5[symbol]),
            vol_30s=sum(e[1] for e in self._q30[symbol]),
            vol_1min_ask=sum(e[1] for e in self._q1_ask[symbol]),
        )

    def update(self, symbol: str, total_volume: int) -> "TrackResult | None":
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
        self._q30[symbol].append(event)
        _trim(self._q1[symbol],  now - timedelta(seconds=60))
        _trim(self._q5[symbol],  now - timedelta(seconds=300))
        _trim(self._q30[symbol], now - timedelta(seconds=30))

        meta = self._meta[symbol]
        return TrackResult(
            symbol=symbol,
            underlying=meta["underlying"],
            contract_type=meta["contract_type"],
            delta=meta["delta"],
            vol_delta=vol_delta,
            vol_1min=sum(e[1] for e in self._q1[symbol]),
            vol_5min=sum(e[1] for e in self._q5[symbol]),
            vol_30s=sum(e[1] for e in self._q30[symbol]),
        )

    def get_vol_1min(self, symbol: str) -> int:
        now = datetime.now()
        cutoff = now - timedelta(seconds=60)
        q = self._q1.get(symbol)
        if not q:
            return 0
        _trim(q, cutoff)
        return sum(e[1] for e in q)

    def get_vol_30s(self, symbol: str) -> int:
        """Total acumulado en los últimos 30 segundos."""
        now = datetime.now()
        cutoff = now - timedelta(seconds=30)
        q = self._q30.get(symbol)
        if not q:
            return 0
        _trim(q, cutoff)
        return sum(e[1] for e in q)

    def get_ask_vol_1min(self, symbol: str) -> int:
        """Volumen de los últimos 60s ejecutado en el ask (price >= ask)."""
        now = datetime.now()
        cutoff = now - timedelta(seconds=60)
        q = self._q1_ask.get(symbol)
        if not q:
            return 0
        _trim(q, cutoff)
        return sum(e[1] for e in q)

    def get_vol_2min_aggressive(self, symbol: str) -> int:
        """Volume in last 120s executed at or above ask."""
        now = datetime.now()
        cutoff = now - timedelta(seconds=120)
        q = self._q2_agg.get(symbol)
        if not q:
            return 0
        _trim(q, cutoff)
        return sum(e[1] for e in q)

    def get_ask_ratio_1min(self, symbol: str) -> float:
        """Ratio ask_vol_1min / vol_1min. Returns 0.0 if vol_1min == 0."""
        vol = self.get_vol_1min(symbol)
        if vol == 0:
            return 0.0
        return self.get_ask_vol_1min(symbol) / vol

    def get_vol_window(self, symbol: str, seconds: int) -> int:
        if seconds > 300:
            raise ValueError(
                "max window is 300s — use get_vol_30s() or get_vol_1min() for shorter windows"
            )
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
        self._q30.clear()
        self._q1_ask.clear()
        self._q2_agg.clear()
        self._meta.clear()


def _trim(q: deque, cutoff: datetime) -> None:
    while q and q[0][0] < cutoff:
        q.popleft()
