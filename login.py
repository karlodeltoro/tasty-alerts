"""
login.py — Autenticación inicial con Tastytrade y guardado de sesión.

Ejecutar UNA sola vez (o cuando la sesión expire):
  python login.py

Tastytrade enviará un código OTP a tu email/SMS en el primer login desde
un dispositivo nuevo. Ingresa el código cuando lo pida.

Genera session.json con la sesión serializada. El sistema principal
(main.py) la reutiliza automáticamente sin pedir OTP.
"""
import os
import sys

from dotenv import load_dotenv
from tastytrade.session import Session

load_dotenv()

SESSION_FILE = os.path.join(os.path.dirname(__file__), "session.json")


def main():
    username = os.getenv("TT_USERNAME")
    password = os.getenv("TT_PASSWORD")

    if not username or not password:
        print("Error: TT_USERNAME y TT_PASSWORD deben estar en .env")
        sys.exit(1)

    print(f"Autenticando como {username}...")

    # Paso 1: login sin OTP para que Tastytrade envíe el email con el código
    try:
        session = Session(
            login=username,
            password=password,
            remember_me=True,
        )
        print("Sesión creada sin OTP (dispositivo ya autorizado).")
    except Exception as e:
        err_str = str(e).lower()
        if any(k in err_str for k in ("two", "factor", "otp", "verification", "401", "device", "challenge")):
            print("Tastytrade requiere verificación. Revisa tu email — se acaba de enviar un código OTP.")
            otp = input("Ingresa el código OTP: ").strip()
            if not otp:
                print("OTP requerido.")
                sys.exit(1)
            try:
                session = Session(
                    login=username,
                    password=password,
                    remember_me=True,
                    two_factor_authentication=otp,
                )
            except Exception as e2:
                print(f"Error de autenticación con OTP: {e2}")
                sys.exit(1)
        else:
            print(f"Error de autenticación: {e}")
            sys.exit(1)

    # Guardar sesión serializada
    serialized = session.serialize()
    with open(SESSION_FILE, "w") as f:
        f.write(serialized)

    print(f"\nSesión guardada en {SESSION_FILE}")
    print(f"  Expira: {session.session_expiration}")
    if session.remember_token:
        print(f"  remember_token: {session.remember_token[:8]}...")
        # Actualizar .env con el nuevo remember_token
        from dotenv import set_key
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        set_key(env_path, "TT_REMEMBER_TOKEN", session.remember_token)
        print("  .env actualizado con nuevo remember_token")

    print("\nListo. Ahora puedes correr: python main.py")


if __name__ == "__main__":
    main()
