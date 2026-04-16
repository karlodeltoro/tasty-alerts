"""
api.py — FastAPI server for tasty-alerts.
Runs inside the main asyncio event loop via uvicorn.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from alert_store import store

_ET = ZoneInfo("America/New_York")

app = FastAPI(title="BullCore Alerts API", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(_ET).isoformat()}


@app.get("/api/alerts/recent")
async def recent_alerts(n: int = Query(default=20, ge=1, le=50)):
    """
    Returns the N most recent alerts fired by tasty-alerts.
    Default: 20. Max: 50.
    """
    return {
        "ok": True,
        "count": n,
        "alerts": store.recent(n),
        "timestamp": datetime.now(_ET).isoformat(),
    }
