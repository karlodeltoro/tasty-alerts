"""
api.py — FastAPI server for tasty-alerts.
Runs inside the main asyncio event loop via uvicorn.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader, APIKeyQuery

import config
from core.alert_store import store

if TYPE_CHECKING:
    from core.tasty_stream import TastyAlertSystem

_ET = ZoneInfo("America/New_York")

# Set by main.py at startup. Stays None until the alert system is constructed.
system: "TastyAlertSystem | None" = None


def _session_info() -> dict[str, Any]:
    """Read TT_SESSION_JSON expiry without holding a session reference."""
    b64 = os.getenv("TT_SESSION_JSON", "")
    if not b64:
        return {"expires_at": None, "expires_in_hours": None}
    try:
        data = json.loads(base64.b64decode(b64).decode())
        exp_str = (
            data.get("session-expiration")
            or data.get("session_expiration")
            or data.get("expiration")
        )
        if not exp_str:
            return {"expires_at": None, "expires_in_hours": None}
        exp = datetime.fromisoformat(str(exp_str).replace("Z", "+00:00"))
        remaining = (exp - datetime.now(timezone.utc)).total_seconds() / 3600.0
        return {"expires_at": exp.isoformat(), "expires_in_hours": round(remaining, 2)}
    except Exception:
        return {"expires_at": None, "expires_in_hours": None}

app = FastAPI(title="BullCore Alerts API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)
_API_KEY_QUERY  = APIKeyQuery(name="api_key", auto_error=False)


async def _verify_api_key(
    header_key: str | None = Security(_API_KEY_HEADER),
    query_key:  str | None = Security(_API_KEY_QUERY),
) -> None:
    if not config.API_KEY:
        return  # no key configured — open access (dev mode)
    if header_key == config.API_KEY or query_key == config.API_KEY:
        return
    raise HTTPException(status_code=403, detail="Invalid or missing API key")


@app.get("/health")
async def health():
    now_iso = datetime.now(_ET).isoformat()

    if system is None:
        # Booting — uvicorn is up but main() hasn't wired the system yet.
        return {
            "status": "starting",
            "timestamp": now_iso,
            "stream":  {"connected": False},
            "session": _session_info(),
            "alerts":  {"total_sent": store.total_sent(), "last_alert_at": None, "last_alert_type": None},
            "schwab":  {"connected": False, "last_tick_at": None},
        }

    last_alert = store.latest()
    macro_ctx = system._macro_stream.get_context() if system._macro_stream is not None else None

    return {
        "status": "ok",
        "timestamp": now_iso,
        "stream": {
            "connected":      system._streamer is not None,
            "contracts":      len(system._active_symbols),
            "trades_per_min": system._trades_per_min,
            "quotes_per_min": system._quotes_per_min,
            "greeks_per_min": system._greeks_per_min,
            "trades_total":   system._trade_count,
            "quotes_total":   system._quote_count,
            "last_heartbeat": (
                system._last_heartbeat_at.isoformat()
                if system._last_heartbeat_at is not None else None
            ),
        },
        "session": _session_info(),
        "alerts": {
            "total_sent":      store.total_sent(),
            "last_alert_at":   last_alert["timestamp"]  if last_alert else None,
            "last_alert_type": last_alert["alert_type"] if last_alert else None,
        },
        "schwab": {
            "connected":    macro_ctx is not None and macro_ctx.stream_type == "LIVE",
            "stream_type":  macro_ctx.stream_type if macro_ctx is not None else "DISABLED",
            "last_tick_at": (
                macro_ctx.last_updated.isoformat()
                if macro_ctx is not None and macro_ctx.last_updated is not None else None
            ),
        },
    }


@app.get("/api/alerts/recent")
async def recent_alerts(
    n: int = Query(default=20, ge=1, le=50),
    _: None = Depends(_verify_api_key),
):
    """
    Returns the N most recent alerts fired by tasty-alerts.
    Default: 20. Max: 50.
    Requires X-API-Key header or ?api_key= query param when BULLCORE_API_KEY is set.
    """
    alerts = store.recent(n)
    return {
        "ok": True,
        "count": len(alerts),
        "alerts": alerts,
        "timestamp": datetime.now(_ET).isoformat(),
    }
