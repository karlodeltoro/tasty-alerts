"""
api.py — FastAPI server for tasty-alerts.
Runs inside the main asyncio event loop via uvicorn.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security.api_key import APIKeyHeader, APIKeyQuery

import config
from alert_store import store

_ET = ZoneInfo("America/New_York")

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
    return {"status": "ok", "timestamp": datetime.now(_ET).isoformat()}


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
