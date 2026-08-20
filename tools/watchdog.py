#!/usr/bin/env python3
"""
T9 — Vigilante: limites operativos, heartbeat y resumen diario.

Corre AL LADO del bot, no dentro. Esa separacion es deliberada: la mitad de las
cosas que hay que vigilar son precisamente las que ocurren cuando el bot deja
de funcionar. Un vigilante que vive dentro del proceso vigilado no sirve para
detectar que el proceso murio.

Que comprueba en cada pasada
----------------------------
1. **Heartbeat** — si el bot lleva mas de 10 minutos sin cerrar un ciclo, o si
   no responde, avisa por Telegram.
2. **Perdida diaria** — al superar el 3 % del equity de inicio del dia, PAUSA
   el bot: deja de abrir, pero sigue gestionando las posiciones abiertas. Se
   usa `/pause` y no `/stop` a proposito: un bot detenido deja de mover el
   trailing y de ejecutar los stops de lo que ya tiene abierto.
3. **Drawdown total** — al superar el 10 % desde el maximo historico de equity,
   dispara el kill switch: cierra todo y detiene el bot.
4. **Resumen diario** — una vez al dia envia por Telegram el estado de la cuenta.

Por que duplicar las protecciones de Freqtrade
----------------------------------------------
La estrategia ya declara `MaxDrawdown` y `StoplossGuard`. Esas protecciones
corren dentro del bot y usan su propia contabilidad. Este vigilante calcula los
limites por su cuenta, desde la API, y actua aunque el bot este atascado. Las
reglas de riesgo del plan se defienden en mas de una capa a proposito: la capa
que falla nunca es la que esperabas.

Uso:
    python tools/watchdog.py --once              # una pasada (para cron)
    python tools/watchdog.py --intervalo 300     # bucle cada 5 minutos
    python tools/watchdog.py --once --simular    # sin actuar, solo reportar
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_freqtrade import ClienteFreqtrade, ErrorAPI, cargar_env  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
ESTADO = RAIZ / "user_data" / "watchdog_estado.json"

# --- Limites (seccion 3 del plan) ------------------------------------------
# Se importan del mismo modulo que usa la estrategia: si alguien cambia una
# cifra alli, el vigilante cambia con ella. Dos copias de un umbral acaban
# divergiendo siempre.
sys.path.insert(0, str(RAIZ / "user_data" / "strategies"))
from reglas_riesgo import DRAWDOWN_TOTAL_MAXIMO, PERDIDA_DIARIA_MAXIMA  # noqa: E402

MINUTOS_SIN_LATIDO = 10   # umbral de alerta de heartbeat


# ===========================================================================
# Telegram — canal independiente del bot
# ===========================================================================

def enviar_telegram(mensaje: str, silencioso: bool = False) -> bool:
    """Manda un mensaje por Telegram sin pasar por el bot.

    Es lo que permite avisar de que el bot esta caido: si el aviso dependiera
    del propio bot, ese caso concreto —el mas importante— no llegaria nunca.
    """
    cargar_env()
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        if not silencioso:
            print("  (Telegram sin configurar: TELEGRAM_TOKEN / TELEGRAM_CHAT_ID)")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": mensaje, "parse_mode": "Markdown"},
            timeout=15)
        return r.ok
    except requests.RequestException as exc:
        print(f"  (fallo al enviar a Telegram: {exc})", file=sys.stderr)
        return False


# ===========================================================================
# Estado persistente
# ===========================================================================

def cargar_estado() -> dict:
    """Estado entre pasadas: pico de equity, dia en curso, alertas ya enviadas."""
    if ESTADO.exists():
        try:
            return json.loads(ESTADO.read_text())
        except json.JSONDecodeError:
            print("  (estado corrupto, se reinicia)", file=sys.stderr)
    return {
        "pico_equity": None,
        "dia_actual": None,
        "equity_inicio_dia": None,
        "ultimo_resumen": None,
        "alertas_enviadas": {},
        "bloqueo_diario_activo": False,
        "kill_switch_disparado": None,
        "fallos_seguidos": 0,
    }


def guardar_estado(estado: dict) -> None:
    ESTADO.parent.mkdir(parents=True, exist_ok=True)
    ESTADO.write_text(json.dumps(estado, indent=2, ensure_ascii=False, default=str),
                      encoding="utf-8")


def alerta_una_vez(estado: dict, clave: str, mensaje: str, horas: int = 6) -> bool:
    """Envia una alerta como maximo una vez cada `horas`.

    Sin esto, un bot caido genera una notificacion cada pasada. A la tercera se
    silencia el chat, y a partir de ahi las alertas dejan de existir.
    """
    ahora = datetime.now(timezone.utc)
    previa = estado["alertas_enviadas"].get(clave)
    if previa:
        if ahora - datetime.fromisoformat(previa) < timedelta(hours=horas):
            return False
    if enviar_telegram(mensaje):
        estado["alertas_enviadas"][clave] = ahora.isoformat()
        return True
    return False


# ===========================================================================
# Comprobaciones
# ===========================================================================

def equity_actual(cliente: ClienteFreqtrade) -> float | None:
    """Valor total de la cuenta en moneda de cotizacion."""
    try:
        b = cliente.balance()
        return float(b.get("total", 0.0))
    except ErrorAPI:
        return None


# Fallos consecutivos que hacen falta para avisar de que el bot no responde.
#
# Con uno solo, cada reinicio de la pila genera una falsa alarma: los
# contenedores arrancan a la vez y el vigilante pregunta antes de que la API
# este escuchando. Exigiendo dos pasadas seguidas (2 x 300 s = 10 min) el aviso
# coincide ademas con el umbral de heartbeat del plan.
#
# Una alerta que salta sin motivo se acaba ignorando, y a partir de ese momento
# deja de proteger de nada.
FALLOS_ANTES_DE_AVISAR = 2


def comprobar_heartbeat(cliente: ClienteFreqtrade, estado: dict) -> bool:
    """True si el bot esta vivo y procesando."""
    try:
        salud = cliente.salud()
    except ErrorAPI as exc:
        estado["fallos_seguidos"] = estado.get("fallos_seguidos", 0) + 1
        seguidos = estado["fallos_seguidos"]

        if seguidos < FALLOS_ANTES_DE_AVISAR:
            print(f"  heartbeat: sin respuesta ({seguidos}/{FALLOS_ANTES_DE_AVISAR}) — "
                  "puede ser un reinicio; se espera a la proxima pasada")
            return False

        alerta_una_vez(estado, "bot_caido",
                       f"🔴 *Bot no responde*\n\n"
                       f"{seguidos} comprobaciones seguidas sin respuesta.\n\n{exc}\n\n"
                       "Si hay posiciones abiertas, revisalas en Binance.\n"
                       "Ver `docs/RUNBOOK.md`.")
        print(f"  heartbeat: SIN RESPUESTA ({seguidos} seguidas) — {exc}")
        return False

    marca = salud.get("last_process")
    if not marca:
        print("  heartbeat: el bot responde pero no reporta ciclos")
        return True

    ultimo = datetime.fromisoformat(str(marca).replace("Z", "+00:00"))
    if ultimo.tzinfo is None:
        ultimo = ultimo.replace(tzinfo=timezone.utc)
    silencio = (datetime.now(timezone.utc) - ultimo).total_seconds() / 60

    if silencio > MINUTOS_SIN_LATIDO:
        alerta_una_vez(estado, "sin_latido",
                       f"🟠 *Bot sin latido*\n\n"
                       f"Ultimo ciclo hace {silencio:.0f} min "
                       f"(umbral {MINUTOS_SIN_LATIDO} min).\n"
                       "El proceso responde pero no esta procesando velas.")
        print(f"  heartbeat: ATASCADO — sin ciclo desde hace {silencio:.0f} min")
        return False

    estado["fallos_seguidos"] = 0
    estado["alertas_enviadas"].pop("sin_latido", None)
    estado["alertas_enviadas"].pop("bot_caido", None)
    print(f"  heartbeat: OK (ultimo ciclo hace {silencio:.1f} min)")
    return True


def comprobar_perdida_diaria(cliente: ClienteFreqtrade, estado: dict,
                             equity: float, simular: bool) -> bool:
    """Al superar el 3 % de perdida en el dia, deja de abrir posiciones."""
    hoy = date.today().isoformat()

    # Nuevo dia: se fija la referencia y se levanta el bloqueo anterior.
    if estado["dia_actual"] != hoy:
        estado["dia_actual"] = hoy
        estado["equity_inicio_dia"] = equity
        if estado["bloqueo_diario_activo"]:
            estado["bloqueo_diario_activo"] = False
            enviar_telegram(
                "🟢 *Nuevo dia*\n\nSe levanta el bloqueo por perdida diaria.\n"
                "El bot puede volver a abrir posiciones — mandale `/start` "
                "si sigue pausado.")
        print(f"  dia nuevo: equity de referencia {equity:,.2f}")
        return True

    referencia = estado.get("equity_inicio_dia") or equity
    if referencia <= 0:
        return True

    variacion = (equity - referencia) / referencia
    print(f"  perdida diaria: {variacion:+.2%} (limite {-PERDIDA_DIARIA_MAXIMA:.2%})")

    if variacion > -PERDIDA_DIARIA_MAXIMA:
        return True

    if estado["bloqueo_diario_activo"]:
        return False   # ya bloqueado, no repetir

    mensaje = (f"🟠 *Limite de perdida diaria alcanzado*\n\n"
               f"Perdida hoy: *{variacion:.2%}* (limite {PERDIDA_DIARIA_MAXIMA:.0%})\n"
               f"Equity: {equity:,.2f} (inicio del dia: {referencia:,.2f})\n\n"
               "El bot queda *pausado*: no abrira posiciones nuevas, pero sigue "
               "gestionando las abiertas (trailing y stop activos).\n"
               "*No se reactiva solo:* revisa que paso antes de darle a `/start`.")
    print(f"  LIMITE DIARIO SUPERADO ({variacion:.2%})")

    if simular:
        print("  [simulacion] se habria pausado el bot")
        return False

    # PAUSAR, no detener: las posiciones abiertas tienen que seguir
    # gestionandose. Un bot detenido no mueve el trailing ni ejecuta los stops,
    # y el limite diario se convertiria en un riesgo mayor que el que evita.
    try:
        cliente.pausar()
        estado["bloqueo_diario_activo"] = True
        enviar_telegram(mensaje)
        print("  bot pausado (no abrira posiciones; las abiertas siguen gestionadas)")
    except ErrorAPI as exc:
        enviar_telegram(f"🔴 *No se pudo pausar el bot*\n\n{mensaje}\n\nError: {exc}")
        print(f"  ERROR al pausar: {exc}", file=sys.stderr)
    return False


def comprobar_drawdown(cliente: ClienteFreqtrade, estado: dict,
                       equity: float, simular: bool) -> bool:
    """Al superar el 10 % de drawdown desde el maximo, dispara el kill switch."""
    pico = estado.get("pico_equity")
    if pico is None or equity > pico:
        estado["pico_equity"] = equity
        pico = equity

    drawdown = (pico - equity) / pico if pico > 0 else 0.0
    print(f"  drawdown total: {drawdown:.2%} "
          f"(pico {pico:,.2f} → actual {equity:,.2f}, limite {DRAWDOWN_TOTAL_MAXIMO:.0%})")

    if drawdown < DRAWDOWN_TOTAL_MAXIMO:
        return True

    # Enclavamiento: un kill switch que se redispara en cada pasada manda una
    # alerta cada 5 minutos hasta que alguien silencia el chat, y cursa ordenes
    # de cierre sobre una cuenta que ya no tiene posiciones. Una vez disparado,
    # se queda disparado hasta que un humano borre el estado a mano — que es
    # justo la barrera que se quiere: reiniciar exige haber mirado que paso.
    if estado.get("kill_switch_disparado"):
        print(f"  kill switch YA DISPARADO el {estado['kill_switch_disparado']} — "
              "el bot sigue detenido")
        print(f"  para rearmar: borra {ESTADO} despues de diagnosticar el incidente")
        return False

    mensaje = (f"🔴 *KILL SWITCH — drawdown maximo superado*\n\n"
               f"Drawdown: *{drawdown:.2%}* (limite {DRAWDOWN_TOTAL_MAXIMO:.0%})\n"
               f"Pico: {pico:,.2f} → actual: {equity:,.2f}\n\n"
               "Cerrando todas las posiciones y deteniendo el bot.\n\n"
               "*No reinicies sin entender que paso.* Reiniciar sin diagnostico "
               "repite la perdida.")
    print(f"  DRAWDOWN MAXIMO SUPERADO ({drawdown:.2%}) — kill switch")
    enviar_telegram(mensaje)

    if simular:
        print("  [simulacion] se habria disparado el kill switch")
        return False

    estado["kill_switch_disparado"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")

    # Se le pasa la URL explicitamente: cuando el vigilante corre en su propio
    # contenedor, `127.0.0.1` es el vigilante y no el bot. Sin esto, el kill
    # switch fallaria justo en el momento en que hace falta.
    r = subprocess.run(
        [sys.executable, str(RAIZ / "tools" / "kill_switch.py"),
         "--url", cliente.base_url, "--confirm",
         "--motivo", f"drawdown {drawdown:.2%} > {DRAWDOWN_TOTAL_MAXIMO:.0%}"],
        cwd=RAIZ, capture_output=True, text=True)
    print("  " + "\n  ".join(r.stdout.splitlines()[-8:]))

    if r.returncode != 0:
        enviar_telegram("🔴 *El kill switch no pudo completarse.*\n\n"
                        "CIERRA LAS POSICIONES A MANO EN BINANCE AHORA.")
    return False


def enviar_resumen_diario(cliente: ClienteFreqtrade, estado: dict) -> None:
    """Resumen diario por Telegram (definicion de hecho de T8)."""
    hoy = date.today().isoformat()
    if estado.get("ultimo_resumen") == hoy:
        return

    try:
        beneficio = cliente.beneficio()
        balance = cliente.balance()
        abiertas = cliente.posiciones_abiertas()
        diario = cliente.resumen_diario(2)
    except ErrorAPI as exc:
        print(f"  resumen diario: no se pudo componer ({exc})")
        return

    dias = diario.get("data", [])
    ayer = dias[1] if len(dias) > 1 else (dias[0] if dias else {})
    moneda = balance.get("stake", "USDT")

    lineas = [
        f"📊 *Resumen diario* — {hoy}",
        "",
        f"Equity: *{balance.get('total', 0):,.2f} {moneda}*",
        f"Beneficio acumulado: *{beneficio.get('profit_closed_percent', 0):+.2f} %* "
        f"({beneficio.get('profit_closed_coin', 0):+,.2f} {moneda})",
        f"Operaciones cerradas: {beneficio.get('closed_trade_count', 0)}",
        f"Win rate: {beneficio.get('winrate', 0) * 100:.1f} %",
        "",
        f"Ayer: {ayer.get('abs_profit', 0):+,.2f} {moneda} "
        f"en {ayer.get('trade_count', 0)} operaciones",
        f"Posiciones abiertas: {len(abiertas)}",
    ]
    for t in abiertas:
        lineas.append(f"  · {t['pair']} {(t.get('profit_ratio') or 0) * 100:+.2f} %")

    if estado.get("bloqueo_diario_activo"):
        lineas += ["", "⚠️ *Bloqueado por perdida diaria.* No abrira posiciones."]

    if enviar_telegram("\n".join(lineas)):
        estado["ultimo_resumen"] = hoy
        print("  resumen diario enviado")
    else:
        print("  resumen diario no enviado (Telegram sin configurar)")


# ===========================================================================

def pasada(cliente: ClienteFreqtrade, simular: bool) -> int:
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ahora}] vigilante")

    estado = cargar_estado()
    vivo = comprobar_heartbeat(cliente, estado)

    if not vivo:
        guardar_estado(estado)
        return 1

    equity = equity_actual(cliente)
    if equity is None:
        print("  no se pudo leer el balance; se omiten los limites en esta pasada")
        guardar_estado(estado)
        return 1

    # El drawdown total se comprueba ANTES que la perdida diaria: es el limite
    # mas grave y su respuesta (cerrar todo) engloba a la del otro (dejar de
    # abrir). Al reves, se detendria el bot y luego el kill switch actuaria
    # sobre un bot ya parado.
    ok_dd = comprobar_drawdown(cliente, estado, equity, simular)
    ok_diario = comprobar_perdida_diaria(cliente, estado, equity, simular) if ok_dd else False

    enviar_resumen_diario(cliente, estado)
    guardar_estado(estado)
    return 0 if (ok_dd and ok_diario) else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Vigilante de limites operativos")
    p.add_argument("--url", default="http://127.0.0.1:8080")
    p.add_argument("--once", action="store_true", help="una sola pasada (cron)")
    p.add_argument("--intervalo", type=int, default=300, help="segundos entre pasadas")
    p.add_argument("--simular", action="store_true",
                   help="comprueba y reporta, pero no detiene ni cierra nada")
    args = p.parse_args()

    cliente = ClienteFreqtrade(args.url)

    if args.once:
        return pasada(cliente, args.simular)

    print(f"Vigilante en marcha — una pasada cada {args.intervalo} s. Ctrl-C para salir.")
    print(f"Limites: perdida diaria {PERDIDA_DIARIA_MAXIMA:.0%} · "
          f"drawdown total {DRAWDOWN_TOTAL_MAXIMO:.0%} · "
          f"heartbeat {MINUTOS_SIN_LATIDO} min")
    if args.simular:
        print("MODO SIMULACION: no se detendra ni cerrara nada.")
    print()
    try:
        while True:
            try:
                pasada(cliente, args.simular)
            except Exception as exc:            # noqa: BLE001
                # Un vigilante que se cae por un error puntual deja de vigilar
                # justo cuando mas falta hace. Se registra y se sigue.
                print(f"  error en la pasada: {exc}", file=sys.stderr)
            print()
            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        print("\nVigilante detenido.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
