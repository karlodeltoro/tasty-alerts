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
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")


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
            return "Macro unavailable"

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
            return "Macro loading..."

        prefix = "[STALE] " if self.stream_type == "STALE" else ""
        return prefix + " | ".join(parts)


def _resolve_token_path() -> str:
    """
    Resolve the Schwab token file path.
    - SCHWAB_TOKEN_JSON: write JSON string to a tempfile and return path
    - SCHWAB_TOKEN_PATH: return path directly
    - Neither set: raise EnvironmentError
    """
    token_json = os.environ.get("SCHWAB_TOKEN_JSON", "")
    if token_json:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        tmp.write(token_json)
        tmp.close()
        return tmp.name

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
        import schwab

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

        logger.info("[SCHWAB] Stream connected — subscribing macro symbols")
        async with self._lock:
            self._context = dataclasses.replace(
                self._context,
                stream_type="LIVE",
                last_updated=datetime.now(_ET),
            )

        await stream_client.handle_message()  # blocks until disconnect

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
