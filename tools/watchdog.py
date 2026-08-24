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
    """Estado global. Contiene un sub-estado por bot vigilado.

    Cada bot lleva su propio pico de equity, su propio limite diario y su propio
    enclavamiento de kill switch: son cuentas simuladas independientes y mezclar
    sus contabilidades haria que el drawdown de uno bloqueara a los demas.
    """
    if ESTADO.exists():
        try:
            estado = json.loads(ESTADO.read_text())
            estado.setdefault("bots", {})
            estado.setdefault("ultima_pasada", None)
            return estado
        except json.JSONDecodeError:
            print("  (estado corrupto, se reinicia)", file=sys.stderr)
    return {"bots": {}, "ultima_pasada": None, "ultimo_resumen": None}


def estado_de_bot(estado: dict, nombre: str) -> dict:
    """Sub-estado de un bot concreto, creandolo si es la primera vez."""
    return estado["bots"].setdefault(nombre, {
        "pico_equity": None,
        "dia_actual": None,
        "equity_inicio_dia": None,
        "alertas_enviadas": {},
        "bloqueo_diario_activo": False,
        "kill_switch_disparado": None,
        "fallos_seguidos": 0,
    })


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


def detectar_suspension(estado: dict, intervalo: int | None) -> float | None:
    """Devuelve los minutos que el ANFITRION estuvo suspendido, si lo estuvo.

    Como se detecta sin salir del contenedor: el vigilante duerme `intervalo`
    segundos entre pasadas. Si al despertar han pasado muchos mas segundos de
    reloj de los que pidio dormir, no es que el bot se haya colgado — es que la
    maquina entera estuvo parada, el vigilante incluido.

    Importa distinguirlo porque las dos situaciones piden respuestas opuestas:

      * bot colgado  -> el proceso esta roto, hay que reiniciarlo y mirar por que
      * equipo suspendido -> el bot esta perfectamente; lo que falla es correr
        esto en un portatil que se duerme

    En un portatil pasa cada noche. Mandar "el bot esta atascado" cada vez
    entrena a ignorar la alerta, y la proxima vez que sea de verdad tampoco se
    mirara.
    """
    previa = estado.get("ultima_pasada")
    if not previa or not intervalo:
        return None

    transcurrido = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(previa)).total_seconds()
    # Margen generoso: el doble del intervalo mas un minuto. Por debajo de eso
    # puede ser simple lentitud del sistema.
    if transcurrido > intervalo * 2 + 60:
        return (transcurrido - intervalo) / 60
    return None


def comprobar_heartbeat(cliente: ClienteFreqtrade, estado: dict,
                        suspension_min: float | None = None,
                        nombre: str = "bot") -> bool:
    """True si el bot esta vivo y procesando."""
    try:
        salud = cliente.salud()
    except ErrorAPI as exc:
        estado["fallos_seguidos"] = estado.get("fallos_seguidos", 0) + 1
        seguidos = estado["fallos_seguidos"]

        if seguidos < FALLOS_ANTES_DE_AVISAR:
            print(f"    heartbeat: sin respuesta ({seguidos}/{FALLOS_ANTES_DE_AVISAR}) — "
                  "puede ser un reinicio; se espera a la proxima pasada")
            return False

        alerta_una_vez(estado, "bot_caido",
                       f"🔴 *{nombre}: no responde*\n\n"
                       f"{seguidos} comprobaciones seguidas sin respuesta.\n\n{exc}\n\n"
                       "Si hay posiciones abiertas, revisalas en Binance.\n"
                       "Ver `docs/RUNBOOK.md`.")
        print(f"    heartbeat: SIN RESPUESTA ({seguidos} seguidas) — {exc}")
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
        if suspension_min is not None:
            # El vigilante tambien estuvo parado: el bot no se colgo, se paro la
            # maquina. No se avisa de un fallo que no existe; si el bot estuviera
            # roto de verdad, la proxima pasada lo vera sin suspension de por
            # medio y entonces si avisara.
            print(f"    heartbeat: {silencio:.0f} min sin ciclo, pero el equipo estuvo "
                  f"suspendido ~{suspension_min:.0f} min — no es un fallo del bot")
            alerta_una_vez(estado, "equipo_suspendido",
                           f"💤 *{nombre}: el equipo estuvo suspendido*\n\n"
                           f"~{suspension_min:.0f} min sin actividad. El bot dejo de "
                           "procesar velas durante ese rato y ya se recupero.\n\n"
                           "*No es un fallo del bot.* Pero si ocurre con una posicion "
                           "abierta, nadie mueve el trailing ni ejecuta el stop en ese "
                           "intervalo.\n\n"
                           "Solucion de fondo: un VPS. Apaño inmediato: `caffeinate`.",
                           horas=12)
            return False

        alerta_una_vez(estado, "sin_latido",
                       f"🟠 *{nombre}: sin latido*\n\n"
                       f"Ultimo ciclo hace {silencio:.0f} min "
                       f"(umbral {MINUTOS_SIN_LATIDO} min).\n"
                       "El proceso responde pero no esta procesando velas.")
        print(f"    heartbeat: ATASCADO — sin ciclo desde hace {silencio:.0f} min")
        return False

    estado["fallos_seguidos"] = 0
    estado["alertas_enviadas"].pop("sin_latido", None)
    estado["alertas_enviadas"].pop("bot_caido", None)
    estado["alertas_enviadas"].pop("equipo_suspendido", None)
    print(f"    heartbeat: OK (ultimo ciclo hace {silencio:.1f} min)")
    return True


def comprobar_perdida_diaria(cliente: ClienteFreqtrade, estado: dict,
                             equity: float, simular: bool,
                             nombre: str = "bot") -> bool:
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
        print(f"    dia nuevo: equity de referencia {equity:,.2f}")
        return True

    referencia = estado.get("equity_inicio_dia") or equity
    if referencia <= 0:
        return True

    variacion = (equity - referencia) / referencia
    print(f"    perdida diaria: {variacion:+.2%} (limite {-PERDIDA_DIARIA_MAXIMA:.2%})")

    if variacion > -PERDIDA_DIARIA_MAXIMA:
        return True

    if estado["bloqueo_diario_activo"]:
        return False   # ya bloqueado, no repetir

    mensaje = (f"🟠 *{nombre}: limite de perdida diaria*\n\n"
               f"Perdida hoy: *{variacion:.2%}* (limite {PERDIDA_DIARIA_MAXIMA:.0%})\n"
               f"Equity: {equity:,.2f} (inicio del dia: {referencia:,.2f})\n\n"
               "El bot queda *pausado*: no abrira posiciones nuevas, pero sigue "
               "gestionando las abiertas (trailing y stop activos).\n"
               "*No se reactiva solo:* revisa que paso antes de darle a `/start`.")
    print(f"    LIMITE DIARIO SUPERADO ({variacion:.2%})")

    if simular:
        print("    [simulacion] se habria pausado el bot")
        return False

    # PAUSAR, no detener: las posiciones abiertas tienen que seguir
    # gestionandose. Un bot detenido no mueve el trailing ni ejecuta los stops,
    # y el limite diario se convertiria en un riesgo mayor que el que evita.
    try:
        cliente.pausar()
        estado["bloqueo_diario_activo"] = True
        enviar_telegram(mensaje)
        print("    bot pausado (no abrira posiciones; las abiertas siguen gestionadas)")
    except ErrorAPI as exc:
        enviar_telegram(f"🔴 *No se pudo pausar el bot*\n\n{mensaje}\n\nError: {exc}")
        print(f"  ERROR al pausar: {exc}", file=sys.stderr)
    return False


def comprobar_drawdown(cliente: ClienteFreqtrade, estado: dict,
                       equity: float, simular: bool,
                       nombre: str = "bot") -> bool:
    """Al superar el 10 % de drawdown desde el maximo, dispara el kill switch."""
    pico = estado.get("pico_equity")
    if pico is None or equity > pico:
        estado["pico_equity"] = equity
        pico = equity

    drawdown = (pico - equity) / pico if pico > 0 else 0.0
    print(f"    drawdown total: {drawdown:.2%} "
          f"(pico {pico:,.2f} → actual {equity:,.2f}, limite {DRAWDOWN_TOTAL_MAXIMO:.0%})")

    if drawdown < DRAWDOWN_TOTAL_MAXIMO:
        return True

    # Enclavamiento: un kill switch que se redispara en cada pasada manda una
    # alerta cada 5 minutos hasta que alguien silencia el chat, y cursa ordenes
    # de cierre sobre una cuenta que ya no tiene posiciones. Una vez disparado,
    # se queda disparado hasta que un humano borre el estado a mano — que es
    # justo la barrera que se quiere: reiniciar exige haber mirado que paso.
    if estado.get("kill_switch_disparado"):
        print(f"    kill switch YA DISPARADO el {estado['kill_switch_disparado']} — "
              "el bot sigue detenido")
        print(f"    para rearmar: borra {ESTADO} despues de diagnosticar el incidente")
        return False

    mensaje = (f"🔴 *{nombre}: KILL SWITCH — drawdown maximo*\n\n"
               f"Drawdown: *{drawdown:.2%}* (limite {DRAWDOWN_TOTAL_MAXIMO:.0%})\n"
               f"Pico: {pico:,.2f} → actual: {equity:,.2f}\n\n"
               "Cerrando todas las posiciones y deteniendo el bot.\n\n"
               "*No reinicies sin entender que paso.* Reiniciar sin diagnostico "
               "repite la perdida.")
    print(f"    DRAWDOWN MAXIMO SUPERADO ({drawdown:.2%}) — kill switch")
    enviar_telegram(mensaje)

    if simular:
        print("    [simulacion] se habria disparado el kill switch")
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
    print("    " + "\n    ".join(r.stdout.splitlines()[-8:]))

    if r.returncode != 0:
        enviar_telegram("🔴 *El kill switch no pudo completarse.*\n\n"
                        "CIERRA LAS POSICIONES A MANO EN BINANCE AHORA.")
    return False


def enviar_resumen_diario(clientes, estado: dict) -> None:
    """Un solo resumen diario con las cinco estrategias comparadas.

    Comparadas y no por separado: cinco mensajes sueltos no dejan ver lo unico
    que importa cuando se corren varias a la vez, que es cual lo esta haciendo
    mejor y cual esta sangrando.
    """
    hoy = date.today().isoformat()
    if estado.get("ultimo_resumen") == hoy:
        return

    lineas = [f"📊 *Resumen diario* — {hoy}", ""]
    total_equity = 0.0
    total_ops = 0
    hubo_datos = False

    for cliente in clientes:
        nombre = getattr(cliente, "nombre", None) or getattr(cliente, "base_url", "bot")
        try:
            beneficio = cliente.beneficio()
            balance = cliente.balance()
            abiertas = cliente.posiciones_abiertas()
        except ErrorAPI:
            lineas.append(f"· *{nombre}*: sin respuesta")
            continue

        hubo_datos = True
        equity = balance.get("total", 0)
        total_equity += equity
        ops = beneficio.get("closed_trade_count", 0)
        total_ops += ops

        eb = estado_de_bot(estado, nombre)
        marca = " ⏸" if eb.get("bloqueo_diario_activo") else ""
        marca += " 🛑" if eb.get("kill_switch_disparado") else ""

        lineas.append(
            f"· *{nombre}*{marca}: {beneficio.get('profit_closed_percent', 0):+.2f} % "
            f"· {ops} ops · {len(abiertas)} abiertas"
        )
        for t in abiertas:
            lineas.append(f"    {t['pair']} {(t.get('profit_ratio') or 0) * 100:+.2f} %")

    if not hubo_datos:
        return

    lineas += ["", f"Equity total simulada: *{total_equity:,.2f} USDT*",
               f"Operaciones cerradas en total: {total_ops}"]

    if enviar_telegram("\n".join(lineas)):
        estado["ultimo_resumen"] = hoy
        print("  resumen diario enviado")
    else:
        print("  resumen diario no enviado (Telegram sin configurar)")


# ===========================================================================

# ===========================================================================
# Notificaciones de operaciones y comandos de Telegram
# ===========================================================================
#
# Freqtrade trae su propia integracion con Telegram, pero solo sirve con UN bot:
# Telegram admite un unico cliente haciendo getUpdates por token, asi que cinco
# bots con el mismo token se pelean por el canal y responden al azar
# ("Conflict: terminated by other getUpdates").
#
# Por eso el vigilante asume las dos funciones. Ventaja lateral: al hablar con
# los cinco, sus respuestas comparan estrategias en un solo mensaje, que es lo
# que de verdad interesa cuando corren varias a la vez.


def notificar_operaciones(clientes, estado: dict) -> None:
    """Avisa de las operaciones que se han CERRADO desde la ultima pasada.

    Se comparan los identificadores vistos y no las fechas: el reloj del
    contenedor y el del exchange no tienen por que coincidir, y una comparacion
    por tiempo se salta operaciones o las repite.
    """
    vistos = estado.setdefault("operaciones_vistas", {})

    for cliente in clientes:
        nombre = getattr(cliente, "nombre", None) or getattr(cliente, "base_url", "bot")
        try:
            datos = cliente.operaciones_cerradas(200)
        except ErrorAPI:
            continue

        operaciones = datos.get("trades", datos) if isinstance(datos, dict) else datos
        conocidos = set(vistos.get(nombre, []))
        nuevas = [t for t in operaciones if t.get("trade_id") not in conocidos]

        # Primera vez que se ve este bot: se registra el historial sin avisar.
        # Si no, al arrancar el vigilante mandaria un mensaje por cada operacion
        # historica de golpe.
        if not conocidos and operaciones:
            vistos[nombre] = [t.get("trade_id") for t in operaciones]
            continue

        for t in sorted(nuevas, key=lambda x: x.get("close_timestamp") or 0):
            beneficio = (t.get("profit_ratio") or 0) * 100
            absoluto = t.get("profit_abs") or 0
            icono = "🟢" if absoluto > 0 else "🔴"
            minutos = t.get("trade_duration") or 0
            duracion = f"{minutos:.0f} min" if minutos < 120 else f"{minutos/60:.1f} h"

            enviar_telegram(
                f"{icono} *{nombre}* cerro {t.get('pair','')}\n\n"
                f"Resultado: *{beneficio:+.2f} %* ({absoluto:+.2f} USDT)\n"
                f"Duracion: {duracion}\n"
                f"Entrada {t.get('open_rate', 0):,.4f} → salida {t.get('close_rate', 0):,.4f}\n"
                f"Motivo: `{t.get('exit_reason','')}`")

        if nuevas:
            vistos[nombre] = [t.get("trade_id") for t in operaciones]


def _resumen_para_telegram(clientes) -> str:
    """Texto de /estado: los cinco bots y sus posiciones, en un mensaje."""
    lineas = ["📋 *Estado de los bots*", ""]
    total_abiertas = 0
    for cliente in clientes:
        nombre = getattr(cliente, "nombre", "bot")
        try:
            b = cliente.beneficio()
            bal = cliente.balance()
            abiertas = cliente.posiciones_abiertas()
        except ErrorAPI:
            lineas.append(f"· *{nombre}*: sin respuesta")
            continue

        total_abiertas += len(abiertas)
        lineas.append(
            f"· *{nombre}*: {bal.get('total', 0):,.2f} USDT · "
            f"{b.get('profit_closed_percent', 0):+.2f} % · "
            f"{b.get('closed_trade_count', 0)} ops · {len(abiertas)} abiertas")
        for t in abiertas:
            lineas.append(
                f"    {t['pair']} {(t.get('profit_ratio') or 0) * 100:+.2f} % "
                f"(entrada {t['open_rate']:,.4f})")

    if total_abiertas == 0:
        lineas += ["", "_Ninguna posicion abierta ahora mismo._"]
    return "\n".join(lineas)


def _operaciones_para_telegram(clientes, limite: int = 10) -> str:
    todas = []
    for cliente in clientes:
        nombre = getattr(cliente, "nombre", "bot")
        try:
            datos = cliente.operaciones_cerradas(200)
        except ErrorAPI:
            continue
        ops = datos.get("trades", datos) if isinstance(datos, dict) else datos
        for t in ops:
            t["_bot"] = nombre
            todas.append(t)

    if not todas:
        return "Todavia no hay ninguna operacion cerrada."

    todas.sort(key=lambda t: t.get("close_timestamp") or 0, reverse=True)
    ganadas = [t for t in todas if (t.get("profit_abs") or 0) > 0]
    total = sum(t.get("profit_abs") or 0 for t in todas)

    lineas = [f"📊 *Ultimas {min(limite, len(todas))} operaciones*", ""]
    for t in todas[:limite]:
        icono = "🟢" if (t.get("profit_abs") or 0) > 0 else "🔴"
        lineas.append(f"{icono} {t['_bot']} {t.get('pair','')} "
                      f"{(t.get('profit_ratio') or 0)*100:+.2f} %")
    lineas += ["", f"*Total: {len(todas)} cerradas · {len(ganadas)} ganadas / "
                   f"{len(todas)-len(ganadas)} perdidas*",
               f"Win rate {len(ganadas)/len(todas)*100:.1f} % · "
               f"acumulado {total:+.2f} USDT"]
    return "\n".join(lineas)


AYUDA = """🤖 *Comandos*

/estado — que tiene abierto cada bot ahora
/ops — ultimas operaciones cerradas y si se ganaron
/pausar — los cinco dejan de abrir posiciones
/reanudar — vuelven a operar
/ayuda — esto

Las notificaciones de cierre llegan solas."""


def atender_comandos(clientes, estado: dict, timeout: int = 10) -> None:
    """Escucha Telegram y responde. No lanza nunca.

    Un fallo atendiendo comandos no puede tumbar el vigilante: su trabajo
    principal es vigilar limites, y eso tiene que seguir pasando aunque
    Telegram este caido.
    """
    cargar_env()
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    if not token:
        return

    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": estado.get("offset_telegram", 0),
                                 "timeout": timeout},
                         timeout=timeout + 10)
        if not r.ok:
            return
        actualizaciones = r.json().get("result", [])
    except requests.RequestException:
        return

    for act in actualizaciones:
        estado["offset_telegram"] = act["update_id"] + 1
        texto = (act.get("message", {}).get("text") or "").strip().lower()
        if not texto.startswith("/"):
            continue
        comando = texto.split()[0].split("@")[0]

        try:
            if comando in ("/estado", "/status"):
                enviar_telegram(_resumen_para_telegram(clientes))
            elif comando in ("/ops", "/operaciones", "/profit"):
                enviar_telegram(_operaciones_para_telegram(clientes))
            elif comando in ("/pausar", "/pause", "/stop"):
                for c in clientes:
                    try:
                        c.pausar()
                    except ErrorAPI:
                        pass
                enviar_telegram("⏸ Los cinco bots quedan *pausados*: no abriran "
                                "posiciones nuevas.\nLas abiertas siguen "
                                "gestionandose.\n\n/reanudar para volver.")
            elif comando in ("/reanudar", "/start"):
                for c in clientes:
                    try:
                        c.arrancar()
                    except ErrorAPI:
                        pass
                enviar_telegram("▶️ Los cinco bots vuelven a operar.")
            else:
                enviar_telegram(AYUDA)
        except Exception as exc:                  # noqa: BLE001
            print(f"  error atendiendo {comando}: {exc}", file=sys.stderr)


def pasada_bot(cliente: ClienteFreqtrade, estado: dict, nombre: str,
               simular: bool, suspension: float | None) -> int:
    """Comprobaciones de UN bot. Devuelve 0 si todo esta en orden."""
    eb = estado_de_bot(estado, nombre)
    print(f"  [{nombre}]")

    vivo = comprobar_heartbeat(cliente, eb, suspension, nombre)
    if not vivo:
        return 1

    equity = equity_actual(cliente)
    if equity is None:
        print("    no se pudo leer el balance; se omiten los limites")
        return 1

    # El drawdown total se comprueba ANTES que la perdida diaria: es el limite
    # mas grave y su respuesta (cerrar todo) engloba a la del otro.
    ok_dd = comprobar_drawdown(cliente, eb, equity, simular, nombre)
    ok_diario = (comprobar_perdida_diaria(cliente, eb, equity, simular, nombre)
                 if ok_dd else False)
    return 0 if (ok_dd and ok_diario) else 1


def pasada(clientes, simular: bool, intervalo: int | None = None) -> int:
    """Una ronda completa sobre todos los bots vigilados.

    Acepta un cliente suelto o una lista: los tests usan uno, produccion usa
    cinco. Un unico vigilante para todos y no cinco vigilantes en paralelo,
    porque asi el resumen diario llega en un solo mensaje comparando las cinco
    estrategias — que es justo la informacion util cuando corren a la vez.
    """
    if not isinstance(clientes, (list, tuple)):
        clientes = [clientes]

    ahora = datetime.now(timezone.utc)
    print(f"[{ahora:%Y-%m-%d %H:%M:%S} UTC] vigilante — {len(clientes)} bot(s)")

    estado = cargar_estado()
    suspension = detectar_suspension(estado, intervalo)
    if suspension is not None:
        print(f"  el equipo estuvo suspendido ~{suspension:.0f} min desde la ultima pasada")

    problemas = 0
    for cliente in clientes:
        nombre = getattr(cliente, "nombre", None) or getattr(cliente, "base_url", "bot")
        try:
            problemas += pasada_bot(cliente, estado, nombre, simular, suspension)
        except Exception as exc:                     # noqa: BLE001
            # Un bot que falla de forma inesperada no puede dejar sin vigilancia
            # a los otros cuatro.
            print(f"  [{nombre}] error inesperado: {exc}", file=sys.stderr)
            problemas += 1

    notificar_operaciones(clientes, estado)
    enviar_resumen_diario(clientes, estado)
    estado["ultima_pasada"] = datetime.now(timezone.utc).isoformat()
    guardar_estado(estado)
    return 0 if problemas == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Vigilante de limites operativos")
    p.add_argument("--url", action="append", default=None,
                   help="URL de un bot. Repetible: --url http://a:8080 --url http://b:8080. "
                        "Opcionalmente con nombre: nombre=http://host:puerto")
    p.add_argument("--once", action="store_true", help="una sola pasada (cron)")
    p.add_argument("--intervalo", type=int, default=300, help="segundos entre pasadas")
    p.add_argument("--simular", action="store_true",
                   help="comprueba y reporta, pero no detiene ni cierra nada")
    args = p.parse_args()

    urls = args.url or ["http://127.0.0.1:8080"]
    clientes = []
    for entrada in urls:
        # Formato opcional "nombre=url" para que las alertas digan que estrategia
        # fallo y no una URL con puerto, que no le dice nada a nadie a las 3 AM.
        if "=" in entrada and not entrada.startswith("http"):
            nombre, _, url = entrada.partition("=")
        else:
            nombre, url = entrada, entrada
        cliente = ClienteFreqtrade(url)
        cliente.nombre = nombre
        clientes.append(cliente)

    if args.once:
        # Sin bucle no hay pasada previa con la que comparar, asi que no se
        # intenta detectar suspension: se pasa el intervalo igualmente por si
        # se ejecuta desde cron con una cadencia fija.
        return pasada(clientes, args.simular, args.intervalo)

    print(f"Vigilante en marcha — {len(clientes)} bot(s), una pasada cada "
          f"{args.intervalo} s. Ctrl-C para salir.")
    for c in clientes:
        print(f"  · {c.nombre}  ->  {c.base_url}")
    print(f"Limites: perdida diaria {PERDIDA_DIARIA_MAXIMA:.0%} · "
          f"drawdown total {DRAWDOWN_TOTAL_MAXIMO:.0%} · "
          f"heartbeat {MINUTOS_SIN_LATIDO} min")
    if args.simular:
        print("MODO SIMULACION: no se detendra ni cerrara nada.")
    print()
    try:
        while True:
            try:
                pasada(clientes, args.simular, args.intervalo)
            except Exception as exc:            # noqa: BLE001
                # Un vigilante que se cae por un error puntual deja de vigilar
                # justo cuando mas falta hace. Se registra y se sigue.
                print(f"  error en la pasada: {exc}", file=sys.stderr)
            print()
            # En vez de dormir de un tiron, se atiende Telegram cada 10 s. Asi
            # un /estado se responde al momento y no dentro de cinco minutos.
            limite = time.monotonic() + args.intervalo
            while time.monotonic() < limite:
                estado = cargar_estado()
                atender_comandos(clientes, estado)
                guardar_estado(estado)
                time.sleep(2)
    except KeyboardInterrupt:
        print("\nVigilante detenido.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
