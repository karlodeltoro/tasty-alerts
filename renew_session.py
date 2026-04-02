"""
renew_session.py — Renueva la sesión de Tastytrade y actualiza TT_SESSION_JSON en Railway.

Ejecución manual (desde Mac si la renovación automática falla):
  python renew_session.py

Se ejecuta también automáticamente cada 20 horas desde main.py.

Estrategia de autenticación:
  1. remember_token del TT_SESSION_JSON actual (puede funcionar desde Railway)
  2. username/password (solo desde Mac — dispositivo autorizado)
"""
import base64
import json
import logging
import os
import sys

import httpx
from dotenv import load_dotenv
from tastytrade.session import Session

import telegram_notifier as tg

load_dotenv()
logger = logging.getLogger(__name__)

_RAILWAY_API = "https://backboard.railway.app/graphql/v2"

_MUTATION = """
mutation variableUpsert($input: VariableUpsertInput!) {
  variableUpsert(input: $input)
}
"""


def _get_new_session() -> Session:
    """
    Obtiene una nueva sesión autenticada.
    Prueba remember_token primero (portable), luego username/password (solo Mac).
    """
    username = os.environ["TT_USERNAME"]

    session_b64 = os.getenv("TT_SESSION_JSON")
    if session_b64:
        try:
            data = json.loads(base64.b64decode(session_b64).decode())
            remember_token = data.get("remember_token")
            if remember_token:
                logger.info("Renovando con remember_token...")
                session = Session(
                    login=username,
                    remember_token=remember_token,
                    remember_me=True,
                )
                logger.info("Sesion renovada con remember_token.")
                return session
        except Exception as e:
            logger.warning(f"remember_token renewal fallido: {e}")

    logger.info("Renovando con username/password (requiere Mac)...")
    session = Session(
        login=username,
        password=os.environ["TT_PASSWORD"],
        remember_me=True,
    )
    logger.info("Sesion renovada con username/password.")
    return session


def _update_railway(new_b64: str) -> bool:
    """Actualiza TT_SESSION_JSON en Railway via GraphQL API. Devuelve True si exitoso."""
    token    = os.getenv("RAILWAY_API_TOKEN")
    proj_id  = os.getenv("RAILWAY_PROJECT_ID")
    svc_id   = os.getenv("RAILWAY_SERVICE_ID")
    env_id   = os.getenv("RAILWAY_ENVIRONMENT_ID")

    if not all([token, proj_id, svc_id, env_id]):
        logger.warning("Railway vars no configuradas — saltando actualizacion de Railway.")
        return False

    try:
        resp = httpx.post(
            _RAILWAY_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "query": _MUTATION,
                "variables": {
                    "input": {
                        "projectId": proj_id,
                        "serviceId": svc_id,
                        "environmentId": env_id,
                        "name": "TT_SESSION_JSON",
                        "value": new_b64,
                    }
                },
            },
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("errors"):
            logger.error(f"Railway API error: {result['errors']}")
            return False
        logger.info("TT_SESSION_JSON actualizado en Railway.")
        return True
    except Exception as e:
        logger.error(f"Error actualizando Railway: {e}")
        return False


def renew() -> bool:
    """
    Renueva la sesion de Tastytrade y actualiza Railway.
    Actualiza os.environ en memoria para que _make_session use la nueva sesion.
    Devuelve True si exitoso.
    """
    logger.info("Iniciando renovacion de sesion Tastytrade...")
    try:
        session = _get_new_session()
    except Exception as e:
        logger.error(f"No se pudo renovar la sesion: {e}")
        tg.send_session_renewal_failed(str(e))
        return False

    serialized = session.serialize()
    new_b64 = base64.b64encode(serialized.encode()).decode()

    # Actualizar en memoria para el proceso actual
    os.environ["TT_SESSION_JSON"] = new_b64

    railway_ok = _update_railway(new_b64)
    exp = session.session_expiration
    logger.info(f"Sesion renovada. Expira: {exp}. Railway actualizado: {railway_ok}")
    if railway_ok:
        tg.send_session_renewed(exp, railway_ok)
    else:
        tg.send_session_json_manual_update(new_b64, exp)
    return True


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    ok = renew()
    sys.exit(0 if ok else 1)
