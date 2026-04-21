"""
auto_renew.py — Renews TT_SESSION_JSON for tasty-alerts on Railway.

Runs from Mac (authorized device — no OTP needed).
Designed to run as macOS LaunchAgent every 6 hours.

Installation:
  cp scripts/com.bullcore.tastyalerts.autorenew.plist ~/Library/LaunchAgents/
  launchctl load ~/Library/LaunchAgents/com.bullcore.tastyalerts.autorenew.plist
  launchctl start com.bullcore.tastyalerts.autorenew

Dependencies (tasty-alerts venv):
  pip install httpx python-dotenv tastytrade

Required env vars (in scripts/.env):
  TT_USERNAME
  TT_PASSWORD
  RAILWAY_API_TOKEN
  RAILWAY_PROJECT_ID
  RAILWAY_SERVICE_ID
  RAILWAY_ENVIRONMENT_ID

Optional:
  TELEGRAM_BOT_TOKEN
  TELEGRAM_PRIVATE_CHAT_ID
"""
from __future__ import annotations

import base64
import logging
import os
import sys
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


# ─── Telegram ─────────────────────────────────────────────────────────────────

def _tg(text: str) -> None:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id   = os.getenv("TELEGRAM_PRIVATE_CHAT_ID", "")
    if not bot_token or not chat_id:
        logger.warning("Telegram not configured — message not sent.")
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


# ─── Railway ──────────────────────────────────────────────────────────────────

def _update_railway(value: str) -> bool:
    api_token = os.getenv("RAILWAY_API_TOKEN")
    proj_id   = os.getenv("RAILWAY_PROJECT_ID")
    svc_id    = os.getenv("RAILWAY_SERVICE_ID")
    env_id    = os.getenv("RAILWAY_ENVIRONMENT_ID")

    if not all([api_token, proj_id, svc_id, env_id]):
        logger.warning(
            "Railway vars not set — skipping Railway update.\n"
            "Add RAILWAY_API_TOKEN, RAILWAY_PROJECT_ID, RAILWAY_SERVICE_ID, "
            "RAILWAY_ENVIRONMENT_ID to scripts/.env"
        )
        return False

    try:
        resp = httpx.post(
            _RAILWAY_API,
            headers={
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            },
            json={
                "query": _MUTATION,
                "variables": {
                    "input": {
                        "projectId":     proj_id,
                        "serviceId":     svc_id,
                        "environmentId": env_id,
                        "name":          "TT_SESSION_JSON",
                        "value":         value,
                    }
                },
            },
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("errors"):
            logger.error(f"Railway GraphQL error: {result['errors']}")
            return False
        logger.info("TT_SESSION_JSON updated on Railway.")
        return True
    except Exception as e:
        logger.error(f"Railway update failed: {e}")
        return False


# ─── Main ─────────────────────────────────────────────────────────────────────

def run() -> bool:
    username = os.getenv("TT_USERNAME")
    password = os.getenv("TT_PASSWORD")

    if not username or not password:
        msg = "TT_USERNAME or TT_PASSWORD not set."
        logger.error(msg)
        _tg(f"⚠️ *tasty-alerts token renewal failed*: {msg}")
        return False

    logger.info(f"Authenticating as {username}...")
    try:
        session = Session(login=username, password=password, remember_me=True)
        logger.info("Login successful.")
    except Exception as e:
        logger.error(f"Authentication failed: {e}")
        _tg(f"⚠️ *tasty-alerts token renewal failed*:\n`{e}`")
        return False

    serialized = session.serialize()
    new_b64    = base64.b64encode(serialized.encode()).decode()
    expiry     = session.session_expiration

    # Persist locally next to the project root
    session_file = _SCRIPT_DIR.parent / "session.json"
    try:
        session_file.write_text(serialized)
        logger.info(f"Session saved to {session_file}")
    except Exception as e:
        logger.warning(f"Could not write session.json: {e}")

    railway_ok = _update_railway(new_b64)

    if railway_ok:
        logger.info(f"Done. Token expiry: {expiry}. Railway: OK")
        _tg(
            f"🔑 *tasty-alerts — session renewed (Mac)*\n"
            f"Expiry: {expiry}\n"
            f"Railway: ✅"
        )
    else:
        logger.warning(f"Session obtained but Railway NOT updated. Expiry: {expiry}")
        _tg(
            f"⚠️ *tasty-alerts — Railway NOT updated*\n"
            f"Session valid. Expiry: {expiry}\n\n"
            f"Paste this as `TT_SESSION_JSON` in Railway:"
        )
        for i in range(0, len(new_b64), 3800):
            _tg(f"`{new_b64[i:i+3800]}`")

    return True


if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 1)
