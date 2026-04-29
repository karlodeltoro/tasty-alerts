"""
schwab_stream.py — Schwab cross-asset macro context stream.

Runs independently of Tastytrade logic. If Schwab goes down,
tasty-alerts keeps running normally without macro context.

Never import schwab at module level — only inside _connect_and_stream().
"""
import asyncio
import dataclasses
import logging
import os
import tempfile
from collections import defaultdict, deque
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")

_SCHWAB_PKG = None

def _import_schwab_pkg():
    """Load installed schwab-py from site-packages, bypassing local schwab/ package."""
    global _SCHWAB_PKG
    if _SCHWAB_PKG is not None:
        return _SCHWAB_PKG
    import sys, os
    sp = next(
        (p for p in sys.path if 'site-packages' in p
         and os.path.isfile(os.path.join(p, 'schwab', 'auth.py'))),
        None,
    )
    if sp is None:
        raise ImportError("schwab-py not found in site-packages")
    # Evict local schwab/* cache so the installed package can be loaded cleanly
    local = {k: sys.modules.pop(k) for k in list(sys.modules)
             if k == 'schwab' or k.startswith('schwab.')}
    sys.path.insert(0, sp)
    try:
        import schwab as _pkg
    finally:
        sys.path.pop(0)
        # Restore local submodules (schwab_stream, symbol_mapper) but keep installed 'schwab'
        for k, v in local.items():
            if k != 'schwab' and k not in sys.modules:
                sys.modules[k] = v
    _SCHWAB_PKG = _pkg
    return _pkg


@dataclass
class MacroContext:
    vix: float | None = None
    tick: float | None = None
    add: float | None = None        # NYSE Advance-Decline
    uvol: float | None = None       # Up volume
    dvol: float | None = None       # Down volume
    spy: float | None = None
    qqq: float | None = None
    tlt: float | None = None
    hyg: float | None = None
    dxy: float | None = None        # UUP as DXY proxy
    tnx: float | None = None        # 10yr yield
    spy_change_pct: float | None = None
    vix_change_pct: float | None = None
    last_updated: datetime | None = None
    stream_type: str = "LIVE"       # "LIVE", "STALE", or "UNAVAILABLE"

    def format_for_alert(self) -> str:
        """Compact macro string for Telegram alerts. Returns empty-safe string."""
        if self.stream_type == "UNAVAILABLE":
            return ""

        parts = []

        if self.vix is not None:
            vix_str = f"VIX {self.vix:.1f}"
            if self.vix_change_pct is not None:
                arrow = "↑" if self.vix_change_pct >= 0 else "↓"
                vix_str += f" {arrow}{abs(self.vix_change_pct):.1f}%"
            parts.append(vix_str)

        if self.tick is not None:
            parts.append(f"TICK {int(self.tick):+d}")

        if self.add is not None:
            parts.append(f"ADD {int(self.add):+d}")

        if self.tlt is not None:
            parts.append(f"TLT {self.tlt:.1f}")

        if self.hyg is not None:
            parts.append(f"HYG {self.hyg:.2f}")

        if self.dxy is not None:
            parts.append(f"DXY {self.dxy:.2f}")

        if self.tnx is not None:
            parts.append(f"TNX {self.tnx:.2f}%")

        if self.spy is not None:
            spy_str = f"SPY {self.spy:.1f}"
            if self.spy_change_pct is not None:
                arrow = "↑" if self.spy_change_pct >= 0 else "↓"
                spy_str += f" {arrow}{abs(self.spy_change_pct):.1f}%"
            parts.append(spy_str)

        if not parts:
            return ""

        prefix = "[STALE] " if self.stream_type == "STALE" else ""
        return prefix + " | ".join(parts)


@dataclass
class ContractSnapshot:
    schwab_symbol: str
    strike:        float
    expiry:        date
    opt_type:      str          # 'C' or 'P'
    bid:           float = 0.0
    ask:           float = 0.0
    delta:         float = 0.0
    iv:            float = 0.0
    open_interest: int   = 0
    day_volume:    int   = 0
    velocity_1m:   float = 0.0  # contracts added in last 60s
    velocity_5m:   float = 0.0  # contracts/min avg over last 5min
    vol_oi_ratio:  float = 0.0  # day_volume / open_interest
    last_updated:  datetime | None = None

    def enrichment_line(self) -> str:
        """One-line enrichment string for Telegram alerts."""
        parts = []
        underlying = "SPXW" if "SPXW" in self.schwab_symbol else "SPX"
        label = f"{underlying} {int(self.strike)}{'C' if self.opt_type == 'C' else 'P'}"
        vol_str = f"{self.day_volume:,} vol"
        if self.velocity_1m > 0:
            vol_str += f" (+{int(self.velocity_1m)}/min)"
        parts.append(vol_str)
        if self.vol_oi_ratio >= 0.1 and self.open_interest > 0:
            parts.append(f"{self.vol_oi_ratio:.1f}×OI")
        if self.iv > 0:
            parts.append(f"IV {self.iv*100:.1f}%")
        if self.delta != 0:
            parts.append(f"Δ {abs(self.delta):.2f}")
        return f"📊 {label}: {' | '.join(parts)}"


@dataclass
class ContractCandidate:
    snapshot:     ContractSnapshot
    triggered_at: datetime
    reason:       str   # "velocity_spike", "vol_oi_surge", etc.


_TOKEN_TEMP_PATH = os.path.join(tempfile.gettempdir(), "schwab_token_tastyalerts.json")

_OPTION_FIELDS = None   # resolved lazily after schwab import

def _get_option_fields(schwab_mod):
    F = schwab_mod.streaming.StreamClient.LevelOneOptionFields
    return [
        F.BID_PRICE,
        F.ASK_PRICE,
        F.TOTAL_VOLUME,
        F.OPEN_INTEREST,
        F.VOLATILITY,
        F.DELTA,
        F.DAYS_TO_EXPIRATION,
        F.UNDERLYING_PRICE,
        F.MARK,
    ]


def _resolve_token_path() -> str:
    """
    Resolve the Schwab token file path.
    - SCHWAB_TOKEN_JSON: write to a fixed path (overwritten each reconnect, never accumulates)
    - SCHWAB_TOKEN_PATH: return path directly
    - Neither set: raise EnvironmentError
    """
    token_json = os.environ.get("SCHWAB_TOKEN_JSON", "")
    if token_json:
        with open(_TOKEN_TEMP_PATH, "w") as f:
            f.write(token_json)
        return _TOKEN_TEMP_PATH

    token_path = os.environ.get("SCHWAB_TOKEN_PATH", "")
    if token_path:
        return token_path

    raise EnvironmentError(
        "No Schwab token configured — set SCHWAB_TOKEN_JSON or SCHWAB_TOKEN_PATH"
    )


class SchwabMacroStream:
    """
    Background stream that populates MacroContext from Schwab Level 1 quotes.
    Completely independent of Tastytrade — failures are logged and retried,
    never propagated to the caller.
    """

    def __init__(self) -> None:
        self._context: MacroContext = MacroContext(stream_type="UNAVAILABLE")
        self._lock = asyncio.Lock()
        self._running = False
        # Contract monitoring (Level 1 options)
        self._contracts:       dict[str, ContractSnapshot] = {}     # schwab_symbol → snapshot
        self._velocity_hist:   dict[str, deque]            = defaultdict(lambda: deque(maxlen=300))
        self._option_symbols:  set[str]                    = set()  # currently subscribed
        self._candidates:      list[ContractCandidate]     = []
        self._active_client    = None                               # active stream_client ref
        # Config thresholds (loaded from env at start)
        self._velocity_min_1m: float = 0.0
        self._vol_oi_min:      float = 0.0
        self._accel_mult:      float = 0.0

    async def start(self) -> None:
        """Start the stream as a background task. Never raises."""
        self._running = True
        asyncio.create_task(self._run_forever(), name="schwab_macro_stream")

    async def stop(self) -> None:
        self._running = False

    def get_context(self) -> MacroContext:
        """Returns the latest MacroContext snapshot. Always returns immediately."""
        return self._context

    async def _run_forever(self) -> None:
        """Reconnect loop with exponential backoff. Never propagates exceptions."""
        delay = 5
        while self._running:
            try:
                await self._connect_and_stream()
            except Exception as e:
                logger.warning(f"[SCHWAB] Stream error: {e} — reconnecting in {delay}s")
                async with self._lock:
                    self._context = MacroContext(
                        stream_type="STALE",
                        last_updated=self._context.last_updated,
                    )
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
            else:
                delay = 5

    async def _connect_and_stream(self) -> None:
        """Build client, subscribe, and stream until disconnect."""
        schwab = _import_schwab_pkg()

        token_path = _resolve_token_path()
        client = schwab.auth.client_from_token_file(
            token_path,
            api_key=os.environ["SCHWAB_CLIENT_ID"],
            app_secret=os.environ["SCHWAB_CLIENT_SECRET"],
        )
        stream_client = schwab.streaming.StreamClient(client)
        await stream_client.login()

        SYMBOLS = [
            "SPY", "QQQ", "IWM", "TLT", "HYG", "UUP",
            "$VIX.X", "$TICK", "$ADD", "$UVOL", "$DVOL", "$TNX",
        ]
        FIELDS = [
            schwab.streaming.StreamClient.LevelOneEquityFields.LAST_PRICE,
            schwab.streaming.StreamClient.LevelOneEquityFields.CLOSE_PRICE,
            schwab.streaming.StreamClient.LevelOneEquityFields.NET_CHANGE,
            schwab.streaming.StreamClient.LevelOneEquityFields.MARK,
            schwab.streaming.StreamClient.LevelOneEquityFields.NET_CHANGE_PERCENT,
        ]
        await stream_client.level_one_equity_subs(SYMBOLS, fields=FIELDS)
        stream_client.add_level_one_equity_handler(self._handle_quote)

        # Register option handler
        stream_client.add_level_one_option_handler(self._handle_option_quote)

        # Subscribe any option symbols queued before this connection
        self._active_client = stream_client
        if self._option_symbols:
            await self._subscribe_options_to_client(stream_client, list(self._option_symbols))

        logger.info("[SCHWAB] Stream connected — subscribing macro symbols")
        async with self._lock:
            self._context = dataclasses.replace(
                self._context,
                stream_type="LIVE",
                last_updated=datetime.now(_ET),
            )

        await stream_client.handle_message()  # blocks until disconnect
        self._active_client = None

    def _handle_quote(self, msg: dict) -> None:
        """Update MacroContext from a streaming quote message."""
        content = msg.get("content", [])
        updates: dict = {}

        for item in content:
            sym = item.get("key", "")
            last = item.get("3")    # LAST_PRICE
            mark = item.get("33")   # MARK
            chg_pct = item.get("42")  # NET_CHANGE_PERCENT
            price = last if last is not None else mark
            if price is None:
                continue
            price = float(price)

            if sym == "$VIX.X":
                updates["vix"] = price
                if chg_pct is not None:
                    updates["vix_change_pct"] = float(chg_pct)
            elif sym == "$TICK":
                updates["tick"] = price
            elif sym == "$ADD":
                updates["add"] = price
            elif sym == "$UVOL":
                updates["uvol"] = price
            elif sym == "$DVOL":
                updates["dvol"] = price
            elif sym == "$TNX":
                updates["tnx"] = float(price) / 10  # CBOE units → yield %
            elif sym == "SPY":
                updates["spy"] = price
                if chg_pct is not None:
                    updates["spy_change_pct"] = float(chg_pct)
            elif sym == "QQQ":
                updates["qqq"] = price
            elif sym == "TLT":
                updates["tlt"] = price
            elif sym == "HYG":
                updates["hyg"] = price
            elif sym == "UUP":
                updates["dxy"] = price  # UUP as DXY proxy

        if updates:
            updates["last_updated"] = datetime.now(_ET)
            updates["stream_type"] = "LIVE"
            # Update in-place — handler is sync, called from event loop
            for k, v in updates.items():
                object.__setattr__(self._context, k, v)

    async def _subscribe_options_to_client(
        self, stream_client, symbols: list[str]
    ) -> None:
        """Subscribe option symbols in batches of 500."""
        _schwab = _import_schwab_pkg()
        CMD = _schwab.streaming.StreamClient.Command
        fields = _get_option_fields(_schwab)
        for i in range(0, len(symbols), 500):
            batch = symbols[i:i + 500]
            try:
                await stream_client.level_one_option_subs(
                    batch, fields=fields, command=CMD.ADD
                )
            except Exception as e:
                logger.warning(f"[SCHWAB] Option batch sub failed: {e}")
        logger.info(f"[SCHWAB] Subscribed {len(symbols)} option contracts")

    async def subscribe_options(self, symbols: list[str]) -> None:
        """
        Subscribe to Level 1 options for these Schwab OCC symbols.
        Safe to call before or after stream connects.
        Deduplicates — only subscribes symbols not already tracked.
        """
        import os as _os
        self._velocity_min_1m = float(_os.getenv("SCHWAB_VELOCITY_1M_THRESHOLD", "40"))
        self._vol_oi_min      = float(_os.getenv("SCHWAB_VOL_OI_MIN_RATIO", "0.25"))
        self._accel_mult      = float(_os.getenv("SCHWAB_VELOCITY_ACCELERATION", "2.0"))

        new_syms = [s for s in symbols if s not in self._option_symbols]
        if not new_syms:
            return
        self._option_symbols.update(new_syms)
        # Initialize snapshots
        for sym in new_syms:
            if sym not in self._contracts:
                # Parse strike/expiry/type from OCC symbol
                # Format: SPXW  YYMMDD{C|P}SSSSSSSSS
                try:
                    sym_clean = sym.strip()
                    # Find where the date starts (first digit after letters)
                    i = 0
                    while i < len(sym_clean) and (sym_clean[i].isalpha() or sym_clean[i] == ' '):
                        i += 1
                    underlying = sym_clean[:i].strip()
                    rest = sym_clean[i:]  # YYMMDDCSSSSSSS
                    from datetime import date as _date
                    exp = _date(2000 + int(rest[0:2]), int(rest[2:4]), int(rest[4:6]))
                    opt_type = rest[6]
                    strike = int(rest[7:]) / 1000.0
                    self._contracts[sym] = ContractSnapshot(
                        schwab_symbol=sym,
                        strike=strike,
                        expiry=exp,
                        opt_type=opt_type,
                    )
                except Exception:
                    self._contracts[sym] = ContractSnapshot(
                        schwab_symbol=sym, strike=0, expiry=date.today(), opt_type='C'
                    )
        if self._active_client:
            await self._subscribe_options_to_client(self._active_client, new_syms)
        # else: will be subscribed on next _connect_and_stream call

    def _handle_option_quote(self, msg: dict) -> None:
        """Update ContractSnapshot from Schwab Level 1 options message."""
        import time as _t
        now_ts = _t.time()
        content = msg.get("content", [])
        for item in content:
            sym = item.get("key", "")
            if sym not in self._contracts:
                continue
            snap = self._contracts[sym]
            # Field numbers per LevelOneOptionFields enum
            bid = item.get("2");  snap.bid = float(bid) if bid is not None else snap.bid
            ask = item.get("3");  snap.ask = float(ask) if ask is not None else snap.ask
            vol = item.get("8")   # TOTAL_VOLUME
            oi  = item.get("9")   # OPEN_INTEREST
            iv  = item.get("10")  # VOLATILITY (decimal)
            dlt = item.get("28")  # DELTA
            if iv  is not None: snap.iv  = float(iv)
            if dlt is not None: snap.delta = float(dlt)
            if oi  is not None: snap.open_interest = int(oi)
            if vol is not None:
                new_vol = int(vol)
                if new_vol != snap.day_volume:
                    # Track velocity
                    self._velocity_hist[sym].append((now_ts, new_vol))
                    snap.day_volume = new_vol
                    snap.velocity_1m = self._calc_velocity(sym, now_ts, 60)
                    snap.velocity_5m = self._calc_velocity(sym, now_ts, 300) / 5.0
                    snap.vol_oi_ratio = (
                        snap.day_volume / snap.open_interest
                        if snap.open_interest > 0 else 0.0
                    )
                    snap.last_updated = datetime.now(_ET)
                    self._check_candidate(sym, snap)

    def _calc_velocity(self, sym: str, now_ts: float, window_secs: float) -> float:
        """Contracts added within window_secs."""
        hist = self._velocity_hist.get(sym)
        if not hist or len(hist) < 2:
            return 0.0
        cutoff = now_ts - window_secs
        relevant = [(ts, v) for ts, v in hist if ts >= cutoff]
        if len(relevant) < 2:
            return 0.0
        return float(relevant[-1][1] - relevant[0][1])

    def _check_candidate(self, sym: str, snap: ContractSnapshot) -> None:
        """Flag contract as candidate if velocity/vol-OI thresholds exceeded."""
        if self._velocity_min_1m <= 0:
            return
        already = any(c.snapshot.schwab_symbol == sym for c in self._candidates)
        if already:
            return
        is_velocity_spike = snap.velocity_1m >= self._velocity_min_1m
        is_accelerating   = (snap.velocity_5m > 0 and
                             snap.velocity_1m >= snap.velocity_5m * self._accel_mult)
        is_vol_oi_surge   = snap.vol_oi_ratio >= self._vol_oi_min and snap.open_interest > 100
        if is_velocity_spike and (is_accelerating or is_vol_oi_surge):
            reason_parts = []
            if is_velocity_spike:   reason_parts.append(f"velocity {int(snap.velocity_1m)}/min")
            if is_accelerating:     reason_parts.append(f"accel {snap.velocity_1m/snap.velocity_5m:.1f}×")
            if is_vol_oi_surge:     reason_parts.append(f"vol/OI {snap.vol_oi_ratio:.1f}×")
            self._candidates.append(ContractCandidate(
                snapshot=snap,
                triggered_at=datetime.now(_ET),
                reason=", ".join(reason_parts),
            ))
            logger.info(f"[SCHWAB CANDIDATE] {sym} — {reason_parts}")

    def get_contract_enrichment(
        self, strike: float, expiry: date, opt_type: str
    ) -> str:
        """Return enrichment line for a given strike/expiry/type, or empty string."""
        for snap in self._contracts.values():
            if (abs(snap.strike - strike) < 0.5 and
                snap.expiry == expiry and
                snap.opt_type == opt_type and
                snap.day_volume > 0):
                return snap.enrichment_line()
        return ""

    def get_contract_candidates(self) -> list[ContractCandidate]:
        """Return and clear current candidates."""
        cands = list(self._candidates)
        # Remove candidates older than 5 minutes
        cutoff = datetime.now(_ET)
        self._candidates = [
            c for c in self._candidates
            if (cutoff - c.triggered_at).total_seconds() < 300
        ]
        return cands
