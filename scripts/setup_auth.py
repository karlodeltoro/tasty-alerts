"""
setup_auth.py — Obtiene el refresh_token de Tastytrade OAuth.

Ejecutar UNA sola vez para generar las credenciales:
  python setup_auth.py

Necesitas antes:
  1. Registrarte como desarrollador en developer.tastytrade.com
  2. Crear una aplicación OAuth → te dan client_id y client_secret
  3. Tener una cuenta activa en Tastytrade con acceso a futuros

El script abre el navegador para que inicies sesión, captura el código
de autorización y lo intercambia por un refresh_token que guarda en .env

Variables que escribe en .env:
  TT_SECRET  = tu client_secret (provider_secret)
  TT_REFRESH = el refresh_token obtenido
"""
import asyncio
import os
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from dotenv import set_key

# ── Configuración OAuth ───────────────────────────────────────
TASTY_API_URL   = "https://api.tastytrade.com"
REDIRECT_URI    = "http://localhost:9001/callback"
CALLBACK_PORT   = 9001

# ─────────────────────────────────────────────────────────────


class _CallbackHandler(BaseHTTPRequestHandler):
    """Servidor HTTP mínimo para capturar el código de autorización."""
    auth_code: str | None = None

    def do_GET(self):
        qs = parse_qs(urlparse(self.path).query)
        _CallbackHandler.auth_code = qs.get("code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h2>Autorizado! Puedes cerrar esta ventana.</h2>")

    def log_message(self, *args):
        pass  # silenciar logs del servidor


def _run_server(server: HTTPServer):
    server.handle_request()


async def main():
    client_id = input("Ingresa tu client_id (de developer.tastytrade.com): ").strip()
    client_secret = input("Ingresa tu client_secret: ").strip()

    # Construir URL de autorización
    params = {
        "client_id":     client_id,
        "redirect_uri":  REDIRECT_URI,
        "response_type": "code",
    }
    auth_url = f"{TASTY_API_URL}/oauth/authorize?{urlencode(params)}"

    # Arrancar servidor local para capturar el callback
    server = HTTPServer(("localhost", CALLBACK_PORT), _CallbackHandler)
    t = threading.Thread(target=_run_server, args=(server,), daemon=True)
    t.start()

    print(f"\nAbriendo navegador para autorizacion...")
    print(f"Si no se abre, ve manualmente a:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # Esperar hasta que llegue el código
    print("Esperando callback de Tastytrade...", end="", flush=True)
    for _ in range(60):
        await asyncio.sleep(1)
        if _CallbackHandler.auth_code:
            break
        print(".", end="", flush=True)

    code = _CallbackHandler.auth_code
    if not code:
        print("\nTimeout: no se recibió el código de autorización.")
        return

    print(f"\nCódigo recibido: {code[:8]}...")

    # Intercambiar código por tokens
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{TASTY_API_URL}/oauth/token",
            json={
                "grant_type":    "authorization_code",
                "code":          code,
                "client_id":     client_id,
                "client_secret": client_secret,
                "redirect_uri":  REDIRECT_URI,
            },
        )
        if resp.status_code != 200:
            print(f"Error intercambiando código: {resp.status_code} {resp.text}")
            return

        data = resp.json()

    refresh_token = data.get("refresh_token")
    if not refresh_token:
        print(f"No se recibió refresh_token. Respuesta: {data}")
        return

    print("refresh_token obtenido exitosamente.")

    # Guardar en .env
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        open(env_path, "w").close()

    set_key(env_path, "TT_SECRET", client_secret)
    set_key(env_path, "TT_REFRESH", refresh_token)

    print(f"\n.env actualizado:")
    print(f"  TT_SECRET  = {client_secret[:4]}...{client_secret[-4:]}")
    print(f"  TT_REFRESH = {refresh_token[:8]}...")
    print("\nListo! Ahora puedes correr: python main.py")


if __name__ == "__main__":
    asyncio.run(main())
