#!/usr/bin/env python3
"""
Asistente de configuracion de Telegram.

Automatiza lo automatizable: averiguar el chat ID, verificar que el token es
valido, mandar un mensaje de prueba y activar Telegram en el .env.

Lo unico que tienes que hacer a mano es crear el bot con @BotFather y pegar el
token en `.env` — eso no lo puede hacer un script, y tampoco deberia: el token
da control total sobre el bot.

Uso:
    python tools/setup_telegram.py                # asistente completo
    python tools/setup_telegram.py --pegar-token  # introducir el token en oculto
    python tools/setup_telegram.py --probar       # solo comprobar lo configurado
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from pathlib import Path

import requests

RAIZ = Path(__file__).resolve().parents[1]
ENV = RAIZ / ".env"
API = "https://api.telegram.org"


# ---------------------------------------------------------------------------
# Lectura y escritura del .env, preservando comentarios y orden
# ---------------------------------------------------------------------------

def leer_env() -> dict[str, str]:
    valores: dict[str, str] = {}
    if not ENV.exists():
        return valores
    for linea in ENV.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            clave, _, valor = linea.partition("=")
            valores[clave.strip()] = valor.strip().strip('"').strip("'")
    return valores


def escribir_env(clave: str, valor: str) -> None:
    """Actualiza una variable sin tocar el resto del archivo.

    Reescribir el .env entero perderia los comentarios, que en este archivo son
    justo la documentacion de que hace cada variable.
    """
    lineas = ENV.read_text(encoding="utf-8").splitlines()
    patron = re.compile(rf"^\s*{re.escape(clave)}\s*=")
    for i, linea in enumerate(lineas):
        if patron.match(linea):
            lineas[i] = f"{clave}={valor}"
            break
    else:
        lineas.append(f"{clave}={valor}")
    ENV.write_text("\n".join(lineas) + "\n", encoding="utf-8")


def ocultar(token: str) -> str:
    """Nunca imprimir un token entero: acaba en logs, capturas y scrollback."""
    if len(token) < 12:
        return "***"
    return f"{token[:6]}…{token[-4:]}"


def pedir_token() -> int:
    """Pide el token por teclado sin mostrarlo y lo guarda en .env.

    Por que existe este modo en vez de decir "editalo a mano":

      * `getpass` no muestra lo que escribes, asi que el token no queda en la
        pantalla ni en una captura.
      * Al no ser un argumento de linea de comandos, no entra en el historial
        del shell (~/.zsh_history), donde se quedaria en claro para siempre.
      * No pasa por ningun portapapeles compartido ni por ninguna conversacion.

    El token va directo de tu teclado a .env. Nadie mas lo ve por el camino.
    """
    if not ENV.exists():
        print("No existe .env. Crealo primero:  cp .env.example .env", file=sys.stderr)
        return 1

    print("""
Pega el token que te dio @BotFather y pulsa Enter.
No se vera nada mientras escribes — es lo normal, sigue pegando.
""")
    token = getpass.getpass("  TELEGRAM_TOKEN: ").strip().strip('"').strip("'")

    if not token:
        print("\nNo se introdujo nada. No se ha cambiado el .env.", file=sys.stderr)
        return 2

    # Forma de un token de Telegram: <id numerico>:<35 caracteres>
    if not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,50}", token):
        print("\nEso no tiene forma de token de Telegram.\n"
              "Deberia ser algo como  8123456789:AAH... (unos 35 caracteres mas).\n"
              "No se ha cambiado el .env.", file=sys.stderr)
        return 2

    print(f"\n  recibido: {ocultar(token)}")
    print("  verificando contra la API de Telegram…")
    info = verificar_token(token)
    if info is None:
        print("\n  No se guarda un token que Telegram rechaza.", file=sys.stderr)
        return 1

    print(f"  bot: @{info.get('username')}  ({info.get('first_name')})")
    escribir_env("TELEGRAM_TOKEN", token)
    print("  guardado en .env")

    # Si se cambia el token, el chat ID anterior puede ser de otro bot.
    anterior = leer_env().get("TELEGRAM_CHAT_ID", "").strip()
    if anterior:
        print(f"\n  aviso: .env ya tenia TELEGRAM_CHAT_ID={anterior}.")
        print("  Si este token es de un bot distinto, ese chat ID no vale. El")
        print("  asistente lo revisa en el siguiente paso.")

    print(f"""
Siguiente paso — abre el chat con tu bot y pulsa INICIAR:

    https://t.me/{info.get('username')}

Y luego:

    python tools/setup_telegram.py
""")
    return 0


# ---------------------------------------------------------------------------
# Pasos
# ---------------------------------------------------------------------------

def verificar_token(token: str) -> dict | None:
    try:
        r = requests.get(f"{API}/bot{token}/getMe", timeout=15)
    except requests.RequestException as exc:
        print(f"  ERROR de red: {exc}", file=sys.stderr)
        return None

    if r.status_code == 401:
        print("  El token no es valido. Comprueba que lo copiaste entero, sin\n"
              "  espacios y sin comillas. Si dudas, pidele a @BotFather uno nuevo\n"
              "  con /revoke y vuelve a empezar.", file=sys.stderr)
        return None
    if not r.ok:
        print(f"  Telegram respondio {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return None

    return r.json().get("result", {})


def buscar_chat_id(token: str) -> int | None:
    """Busca el chat ID en los mensajes recientes que ha recibido el bot."""
    try:
        r = requests.get(f"{API}/bot{token}/getUpdates", timeout=20)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"  ERROR al consultar getUpdates: {exc}", file=sys.stderr)
        return None

    actualizaciones = r.json().get("result", [])
    if not actualizaciones:
        return None

    # El mas reciente primero: si has escrito desde varios sitios, gana el ultimo.
    for act in reversed(actualizaciones):
        for clave in ("message", "edited_message", "channel_post", "my_chat_member"):
            chat = act.get(clave, {}).get("chat")
            if chat:
                nombre = chat.get("username") or chat.get("first_name") or chat.get("title", "")
                print(f"  encontrado: chat_id={chat['id']}  ({chat.get('type')}"
                      f"{', ' + nombre if nombre else ''})")
                return chat["id"]
    return None


def enviar_prueba(token: str, chat_id: str | int) -> bool:
    mensaje = (
        "✅ *Telegram configurado*\n\n"
        "Este es el canal por el que el bot te avisara de:\n"
        "· aperturas y cierres de posiciones\n"
        "· errores del exchange\n"
        "· el resumen diario\n"
        "· los limites de riesgo (perdida diaria y kill switch)\n\n"
        "Comandos utiles: `/status`, `/profit`, `/daily`, `/pause`, `/forceexit all`"
    )
    try:
        r = requests.post(f"{API}/bot{token}/sendMessage",
                          json={"chat_id": chat_id, "text": mensaje,
                                "parse_mode": "Markdown"},
                          timeout=15)
    except requests.RequestException as exc:
        print(f"  ERROR de red: {exc}", file=sys.stderr)
        return False

    if r.ok:
        return True

    detalle = r.json().get("description", r.text[:200])
    print(f"  Telegram rechazo el mensaje: {detalle}", file=sys.stderr)
    if "chat not found" in detalle.lower():
        print("  Causa habitual: el chat ID no corresponde a este bot, o nunca le\n"
              "  escribiste. Abre el chat con tu bot y mandale /start.", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Asistente de configuracion de Telegram")
    p.add_argument("--probar", action="store_true",
                   help="solo verificar lo ya configurado, sin cambiar nada")
    p.add_argument("--pegar-token", action="store_true",
                   help="introducir el token por teclado sin que se vea ni quede "
                        "en el historial del shell")
    args = p.parse_args()

    if args.pegar_token:
        return pedir_token()

    print("=" * 70)
    print("CONFIGURACION DE TELEGRAM")
    print("=" * 70)

    if not ENV.exists():
        print("\nNo existe .env. Crealo primero:\n  cp .env.example .env", file=sys.stderr)
        return 1

    env = leer_env()
    token = env.get("TELEGRAM_TOKEN", "").strip()

    # --- Paso 1: token ------------------------------------------------------
    print("\n[1/4] Token del bot")
    if not token:
        print("""
  Falta TELEGRAM_TOKEN en .env. Esta parte la tienes que hacer tu:

    1. Abre Telegram y busca  @BotFather  (el que tiene la marca azul)
    2. Mandale  /newbot
    3. Te pedira un nombre visible  ->  por ejemplo: Mi bot de trading
    4. Te pedira un usuario, que DEBE acabar en 'bot'
                                   ->  por ejemplo: nandoski_trading_bot
    5. Te devuelve un token con esta forma:
                                   ->  8123456789:AAH...unas 35 letras mas

    6. Pegalo en .env, en la linea TELEGRAM_TOKEN=

  El token es una credencial: quien lo tenga controla el bot. No lo pegues en
  un chat, ni en una captura, ni lo subas a ningun repositorio.

  Cuando lo tengas, vuelve a ejecutar:  python tools/setup_telegram.py
""")
        return 2

    print(f"  token en .env: {ocultar(token)}")
    print("  verificando contra la API de Telegram…")
    info = verificar_token(token)
    if info is None:
        return 1
    print(f"  bot: @{info.get('username')}  ({info.get('first_name')})")

    # --- Paso 2: chat ID ----------------------------------------------------
    print("\n[2/4] Chat ID")
    chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()

    if chat_id and args.probar:
        print(f"  ya configurado: {chat_id}")
    else:
        print("  buscando en los mensajes recientes del bot…")
        encontrado = buscar_chat_id(token)

        if encontrado is None:
            print(f"""
  El bot no ha recibido ningun mensaje todavia. Telegram no deja que un bot
  escriba primero: tienes que iniciar tu la conversacion.

    1. Abre este enlace:  https://t.me/{info.get('username')}
    2. Pulsa INICIAR  (o escribe /start)
    3. Vuelve a ejecutar:  python tools/setup_telegram.py
""")
            return 2

        if args.probar:
            print(f"  (modo --probar: no se escribe en .env; seria {encontrado})")
            chat_id = str(encontrado)
        elif chat_id and chat_id != str(encontrado):
            print(f"  aviso: .env tenia {chat_id} y se ha encontrado {encontrado}.")
            print(f"  se conserva el de .env. Si quieres el nuevo, cambialo a mano.")
        else:
            escribir_env("TELEGRAM_CHAT_ID", str(encontrado))
            chat_id = str(encontrado)
            print(f"  guardado en .env: TELEGRAM_CHAT_ID={chat_id}")

    # --- Paso 3: mensaje de prueba -----------------------------------------
    print("\n[3/4] Mensaje de prueba")
    if not enviar_prueba(token, chat_id):
        return 1
    print("  enviado. Miralo en Telegram antes de seguir.")

    # --- Paso 4: activar ----------------------------------------------------
    print("\n[4/4] Activar Telegram en el bot")
    activado = env.get("FREQTRADE__TELEGRAM__ENABLED", "false").lower() == "true"
    if args.probar:
        print(f"  FREQTRADE__TELEGRAM__ENABLED={'true' if activado else 'false'} "
              "(modo --probar: sin cambios)")
    elif activado:
        print("  ya estaba activado")
    else:
        escribir_env("FREQTRADE__TELEGRAM__ENABLED", "true")
        print("  activado en .env")

    print("\n" + "=" * 70)
    print("LISTO")
    print("=" * 70)
    print(f"""
  bot     : @{info.get('username')}
  chat id : {chat_id}

  Aplica los cambios reiniciando la pila:

      docker compose up -d --force-recreate

  Deberias recibir un mensaje de arranque. Prueba luego con /status.

  Comprobar sin cambiar nada:  python tools/setup_telegram.py --probar
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
