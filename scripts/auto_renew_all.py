"""
auto_renew_all.py — Universal Tastytrade session renewal for all Railway services.

Authenticates ONCE on Mac (authorized device — no OTP) and pushes the renewed
session to every Railway service that depends on Tastytrade, in parallel.

Services updated:
  1. tasty-alerts       → TT_SESSION_JSON          (full serialized JSON, base64)
  2. bullcore-v2-agent  → TASTYTRADE_SESSION_TOKEN  (raw session token string)

Runs as macOS LaunchAgent every 6 hours via:
  ~/Library/LaunchAgents/com.bullcore.tastyalerts.autorenew.plist

Required env vars (scripts/.env):
  TT_USERNAME, TT_PASSWORD
  RAILWAY_API_TOKEN
  RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID, RAILWAY_ENVIRONMENT_ID  ← tasty-alerts
  TELEGRAM_BOT_TOKEN, TELEGRAM_PRIVATE_CHAT_ID  (optional)
"""
from __future__ import annotations

import base64
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import httpx
from dotenv import load_dotenv
from tastytrade.session import Session

_SCRIPT_DIR = Path(__file__).parent
load_dotenv(_SCRIPT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

_RAILWAY_API = "https://backboard.railway.app/graphql/v2"
_MUTATION = """
mutation variableUpsert($input: VariableUpsertInput!) {
  variableUpsert(input: $input)
}
"""

# ── Service definitions ───────────────────────────────────────────────────────
# Each entry: (label, project_id, service_id, env_id, var_name, value_fn)
# value_fn receives (serialized_json: str, session: Session) → str

def _services(serialized: str, session: Session) -> list[dict]:
    api_token = os.getenv("RAILWAY_API_TOKEN", "")
    b64_value = base64.b64encode(serialized.encode()).decode()

    return [
        {
            "label":          "tasty-alerts",
            "api_token":      api_token,
            "project_id":     os.getenv("RAILWAY_PROJECT_ID", ""),
            "service_id":     os.getenv("RAILWAY_SERVICE_ID", ""),
            "environment_id": os.getenv("RAILWAY_ENVIRONMENT_ID", ""),
            "var_name":       "TT_SESSION_JSON",
            "value":          b64_value,
        },
        {
            "label":          "bullcore-v2-agent",
            "api_token":      api_token,
            "project_id":     "ce24718a-b0ca-4b6a-b73c-cb51c18b784f",
            "service_id":     "2c09ef68-89b9-4359-9f07-ef184143bf26",
            "environment_id": "90f56269-2b9a-4ad9-963e-a527da35bc31",
            "var_name":       "TASTYTRADE_SESSION_TOKEN",
            "value":          session.session_token,
        },
    ]


# ── Railway update ────────────────────────────────────────────────────────────

def _update_railway(svc: dict) -> tuple[str, bool, str]:
    """Push one variable update to Railway. Returns (label, ok, error_msg)."""
    label = svc["label"]
    missing = [k for k in ("api_token", "project_id", "service_id", "environment_id")
               if not svc.get(k)]
    if missing:
        return label, False, f"missing env vars: {missing}"
    try:
        resp = httpx.post(
            _RAILWAY_API,
            headers={
                "Authorization": f"Bearer {svc['api_token']}",
                "Content-Type": "application/json",
            },
            json={
                "query": _MUTATION,
                "variables": {
                    "input": {
                        "projectId":     svc["project_id"],
                        "serviceId":     svc["service_id"],
                        "environmentId": svc["environment_id"],
                        "name":          svc["var_name"],
                        "value":         svc["value"],
                    }
                },
            },
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("errors"):
            return label, False, str(result["errors"])
        logger.info(f"[{label}] {svc['var_name']} updated on Railway.")
        return label, True, ""
    except Exception as e:
        return label, False, str(e)


# ── Telegram ──────────────────────────────────────────────────────────────────

def _tg(text: str) -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.getenv("TELEGRAM_PRIVATE_CHAT_ID", "")
    if not bot_token or not chat_id:
        logger.warning("Telegram not configured — skipping notification.")
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run() -> bool:
    username = os.getenv("TT_USERNAME")
    password = os.getenv("TT_PASSWORD")
    if not username or not password:
        msg = "TT_USERNAME or TT_PASSWORD not set in scripts/.env"
        logger.error(msg)
        _tg(f"⚠️ *Universal TT renewal FAILED*\n{msg}")
        return False

    # ── 1. Authenticate once ──────────────────────────────────────────────────
    logger.info(f"Authenticating as {username}...")
    try:
        session = Session(login=username, password=password, remember_me=True)
        logger.info("Login successful.")
    except Exception as e:
        logger.error(f"Authentication failed: {e}")
        _tg(f"⚠️ *Universal TT renewal FAILED*\nAuth error: `{e}`")
        return False

    serialized = session.serialize()
    expiry     = session.session_expiration

    # Save session.json locally next to project root
    session_file = _SCRIPT_DIR.parent / "session.json"
    try:
        session_file.write_text(serialized)
        logger.info(f"session.json saved to {session_file}")
    except Exception as e:
        logger.warning(f"Could not write session.json: {e}")

    # ── 2. Build service list ─────────────────────────────────────────────────
    services = _services(serialized, session)

    # ── 3. Update all Railway services in parallel ────────────────────────────
    results: dict[str, tuple[bool, str]] = {}  # label → (ok, error)
    with ThreadPoolExecutor(max_workers=len(services)) as pool:
        futures = {pool.submit(_update_railway, svc): svc["label"] for svc in services}
        for future in as_completed(futures):
            label, ok, err = future.result()
            results[label] = (ok, err)
            status = "✅" if ok else f"❌ {err}"
            logger.info(f"  [{label}] Railway: {status}")

    # ── 4. Single Telegram summary ───────────────────────────────────────────
    lines = [f"🔑 *Universal TT renewal — Mac*", f"Expiry: {expiry}", ""]
    all_ok = True
    for svc in services:
        label = svc["label"]
        ok, err = results.get(label, (False, "no result"))
        icon = "✅" if ok else "❌"
        var  = svc["var_name"]
        lines.append(f"{icon} `{label}` → `{var}`")
        if not ok:
            lines.append(f"   Error: {err}")
            all_ok = False

    _tg("\n".join(lines))

    if not all_ok:
        # For any failed tasty-alerts update, send the b64 as fallback
        ta_ok, _ = results.get("tasty-alerts", (True, ""))
        if not ta_ok:
            b64 = base64.b64encode(serialized.encode()).decode()
            _tg("⚠️ *tasty-alerts Railway not updated — paste manually as `TT_SESSION_JSON`:*")
            for i in range(0, len(b64), 3800):
                _tg(f"`{b64[i:i+3800]}`")

    return True


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
