"""
alert_store.py — In-memory alert feed (thread-safe ring buffer).
Max 50 alerts. Written by tasty_stream, read by the FastAPI server.
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


@dataclass
class AlertRecord:
    alert_type: str       # "BLOCK_PRINT", "BLOCK_ACCUM", "SWEEP_BURST", "PRESSURE_COOKER"
    direction: str        # "CALL" or "PUT"
    underlying: str       # "/ES", "/NQ", "/GC"
    strike: float
    expiry_date: str      # ISO format "YYYY-MM-DD"
    vol: int              # contracts
    ask_ratio: float      # 0.0 - 1.0
    delta: float
    iv: float
    bid: float
    ask: float
    macro_context: str    # e.g. "VIX 28.4 | TICK -800 | SPY 541.2"
    timestamp: str = field(default_factory=lambda: datetime.now(_ET).isoformat())
    dte: int = 0


class AlertStore:
    def __init__(self, maxlen: int = 50):
        self._q: deque[AlertRecord] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def push(self, record: AlertRecord) -> None:
        with self._lock:
            self._q.appendleft(record)

    def recent(self, n: int = 20) -> list[dict]:
        with self._lock:
            items = list(self._q)[:n]
        return [asdict(r) for r in items]

    def clear(self) -> None:
        with self._lock:
            self._q.clear()


# Singleton — imported everywhere
store = AlertStore()
