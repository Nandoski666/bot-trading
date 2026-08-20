"""
T9 — Tests de los limites operativos y el kill switch.

Definicion de hecho del ticket: "test que simula una racha de perdidas y
verifica que el bot deja de operar en el umbral correcto".

Se prueba el vigilante con un bot falso cuyo equity se mueve a voluntad. Asi se
puede recorrer una racha de perdidas paso a paso y observar en que momento
exacto actua cada limite — algo imposible de comprobar contra un bot real sin
esperar a que ocurra de verdad.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "tools"))
sys.path.insert(0, str(RAIZ / "user_data" / "strategies"))

import watchdog                                    # noqa: E402
from api_freqtrade import ErrorAPI                 # noqa: E402
from reglas_riesgo import (                        # noqa: E402
    DRAWDOWN_TOTAL_MAXIMO,
    PERDIDA_DIARIA_MAXIMA,
    RIESGO_POR_OPERACION,
)


# ===========================================================================
# Bot falso
# ===========================================================================

class BotFalso:
    """Sustituto del bot: equity manipulable y registro de lo que se le pidio."""

    def __init__(self, equity: float = 10_000.0, vivo: bool = True):
        self.base_url = "http://127.0.0.1:8080"
        self.equity = equity
        self.vivo = vivo
        self.pausado = False
        self.detenido = False
        self.posiciones: list[dict] = []
        self.ultimo_ciclo = datetime.now(timezone.utc)
        self.llamadas: list[str] = []

    # --- lectura ---
    def salud(self) -> dict:
        self.llamadas.append("salud")
        if not self.vivo:
            raise ErrorAPI("bot caido")
        return {"last_process": self.ultimo_ciclo.isoformat()}

    def balance(self) -> dict:
        if not self.vivo:
            raise ErrorAPI("bot caido")
        return {"total": self.equity, "stake": "USDT"}

    def posiciones_abiertas(self) -> list[dict]:
        return self.posiciones

    def beneficio(self) -> dict:
        return {"profit_closed_percent": 0.0, "profit_closed_coin": 0.0,
                "closed_trade_count": 0, "winrate": 0.0}

    def resumen_diario(self, dias: int = 7) -> dict:
        return {"data": [{"abs_profit": 0.0, "trade_count": 0}]}

    # --- escritura ---
    def pausar(self) -> dict:
        self.llamadas.append("pausar")
        self.pausado = True
        return {"status": "pausado"}

    def detener(self) -> dict:
        self.llamadas.append("detener")
        self.detenido = True
        return {"status": "detenido"}

    def cerrar_todo(self) -> dict:
        self.llamadas.append("cerrar_todo")
        self.posiciones = []
        return {"result": "todo cerrado"}


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    """Aisla el estado del vigilante y captura los mensajes de Telegram."""
    monkeypatch.setattr(watchdog, "ESTADO", tmp_path / "watchdog_estado.json")

    mensajes: list[str] = []
    monkeypatch.setattr(watchdog, "enviar_telegram",
                        lambda texto, silencioso=False: mensajes.append(texto) or True)

    disparos: list[list[str]] = []

    class ResultadoFalso:
        returncode = 0
        stdout = "kill switch completado"

    def subproceso_falso(cmd, **kwargs):
        disparos.append(cmd)
        return ResultadoFalso()

    monkeypatch.setattr(watchdog.subprocess, "run", subproceso_falso)
    return {"mensajes": mensajes, "kill_switch": disparos}


# ===========================================================================
# Racha de perdidas — el test que pide la definicion de hecho
# ===========================================================================

def test_racha_de_perdidas_detiene_el_bot_en_el_umbral_diario(entorno):
    """Perdidas de 0.5 % encadenadas: el bot para al cruzar el 3 % diario.

    Con el riesgo por operacion del plan, hacen falta 6 stops seguidos para
    llegar al limite. El test comprueba que para en el sexto y **no antes**:
    un cortacircuitos que salta pronto es tan inutil como uno que no salta,
    porque acaba desactivado.
    """
    equity_inicial = 10_000.0
    bot = BotFalso(equity=equity_inicial)

    # Primera pasada: fija la referencia del dia.
    watchdog.pasada(bot, simular=False)
    assert not bot.pausado

    perdida_por_operacion = equity_inicial * RIESGO_POR_OPERACION   # 50 USDT
    operaciones_hasta_el_limite = int(PERDIDA_DIARIA_MAXIMA / RIESGO_POR_OPERACION)  # 6

    for n in range(1, operaciones_hasta_el_limite + 1):
        bot.equity -= perdida_por_operacion
        watchdog.pasada(bot, simular=False)

        perdida_acumulada = (equity_inicial - bot.equity) / equity_inicial
        if perdida_acumulada < PERDIDA_DIARIA_MAXIMA:
            assert not bot.pausado, (
                f"el bot se pauso tras {n} perdidas ({perdida_acumulada:.2%}), "
                f"antes del umbral del {PERDIDA_DIARIA_MAXIMA:.0%}"
            )
        else:
            assert bot.pausado, (
                f"el bot NO se pauso con una perdida del {perdida_acumulada:.2%}, "
                f"por encima del umbral del {PERDIDA_DIARIA_MAXIMA:.0%}"
            )

    assert bot.pausado
    assert any("perdida diaria" in m.lower() for m in entorno["mensajes"]), \
        "no se aviso por Telegram del limite diario"


def test_el_limite_diario_no_salta_justo_por_debajo(entorno):
    """Una perdida del 2.99 % no detiene el bot. El umbral es el umbral."""
    bot = BotFalso(equity=10_000.0)
    watchdog.pasada(bot, simular=False)

    bot.equity = 10_000.0 * (1 - (PERDIDA_DIARIA_MAXIMA - 0.0001))
    watchdog.pasada(bot, simular=False)

    assert not bot.pausado


def test_el_bloqueo_diario_no_se_repite(entorno):
    """Superado el limite, no se manda una alerta por pasada.

    Un cortacircuitos que notifica cada 5 minutos acaba silenciado, y a partir
    de ahi ya no protege de nada.
    """
    bot = BotFalso(equity=10_000.0)
    watchdog.pasada(bot, simular=False)

    # -4 %: supera el limite diario del 3 % pero se queda por debajo del
    # drawdown del 10 %, que dispararia el kill switch y enmascararia el caso.
    bot.equity = 9_600.0
    for _ in range(5):
        watchdog.pasada(bot, simular=False)

    avisos = [m for m in entorno["mensajes"] if "perdida diaria" in m.lower()]
    assert len(avisos) == 1, f"se enviaron {len(avisos)} avisos del limite diario"


def test_nuevo_dia_levanta_el_bloqueo(entorno, monkeypatch):
    """Al cambiar el dia se reinicia la referencia y se levanta el bloqueo."""
    bot = BotFalso(equity=10_000.0)
    watchdog.pasada(bot, simular=False)

    bot.equity = 9_600.0     # -4 % -> bloquea
    watchdog.pasada(bot, simular=False)
    assert json.loads(watchdog.ESTADO.read_text())["bloqueo_diario_activo"]

    # Manana.
    manana = date.today() + timedelta(days=1)

    class FechaFalsa(date):
        @classmethod
        def today(cls):
            return manana

    monkeypatch.setattr(watchdog, "date", FechaFalsa)
    watchdog.pasada(bot, simular=False)

    estado = json.loads(watchdog.ESTADO.read_text())
    assert estado["bloqueo_diario_activo"] is False
    assert estado["equity_inicio_dia"] == pytest.approx(9_600.0), \
        "la referencia del nuevo dia debe ser el equity actual, no el de ayer"


# ===========================================================================
# Kill switch por drawdown total
# ===========================================================================

def test_drawdown_del_diez_por_ciento_dispara_el_kill_switch(entorno):
    """Al perder el 10 % desde el maximo, se cierra todo y se apaga."""
    bot = BotFalso(equity=10_000.0)
    watchdog.pasada(bot, simular=False)

    # Sube primero: el drawdown se mide desde el MAXIMO, no desde el inicio.
    bot.equity = 12_000.0
    watchdog.pasada(bot, simular=False)
    assert not entorno["kill_switch"]

    # Cae un 9.9 % desde el pico: todavia no.
    bot.equity = 12_000.0 * (1 - DRAWDOWN_TOTAL_MAXIMO + 0.001)
    watchdog.pasada(bot, simular=False)
    assert not entorno["kill_switch"], "el kill switch salto antes del umbral"

    # Cae un 10.1 %: ahora si.
    bot.equity = 12_000.0 * (1 - DRAWDOWN_TOTAL_MAXIMO - 0.001)
    watchdog.pasada(bot, simular=False)
    assert entorno["kill_switch"], "el kill switch NO salto al superar el umbral"

    comando = entorno["kill_switch"][0]
    assert "kill_switch.py" in " ".join(comando)
    assert "--confirm" in comando
    assert any("KILL SWITCH" in m for m in entorno["mensajes"])


def test_el_drawdown_se_mide_desde_el_maximo_no_desde_el_inicio(entorno):
    """Ganar y devolverlo cuenta como drawdown.

    Un sistema que sube un 50 % y luego devuelve el 12 % esta en beneficio
    respecto al inicio, pero ha perdido un 12 % de lo que llego a tener. Ese es
    el numero que importa: mide si el sistema dejo de funcionar.
    """
    bot = BotFalso(equity=10_000.0)
    watchdog.pasada(bot, simular=False)

    bot.equity = 15_000.0
    watchdog.pasada(bot, simular=False)

    bot.equity = 13_000.0    # -13.3 % desde el pico, +30 % desde el inicio
    watchdog.pasada(bot, simular=False)

    assert entorno["kill_switch"], (
        "no salto el kill switch: se esta midiendo el drawdown desde el capital "
        "inicial y no desde el maximo alcanzado"
    )


def test_modo_simulacion_no_toca_nada(entorno):
    """--simular reporta lo que haria, pero no detiene ni cierra."""
    bot = BotFalso(equity=10_000.0)
    watchdog.pasada(bot, simular=True)

    bot.equity = 5_000.0    # -50 %: dispararia todo
    watchdog.pasada(bot, simular=True)

    assert not bot.pausado, "se detuvo el bot en modo simulacion"
    assert not entorno["kill_switch"], "se disparo el kill switch en modo simulacion"


# ===========================================================================
# Heartbeat
# ===========================================================================

def test_un_solo_fallo_no_genera_alerta(entorno):
    """Un fallo aislado es un reinicio, no una caida.

    Los contenedores del bot y del vigilante arrancan a la vez, y el vigilante
    llega a preguntar antes de que la API este escuchando. Avisar en ese caso
    manda una falsa alarma en cada reinicio — y una alerta que salta sin motivo
    se acaba ignorando, momento en el que deja de proteger de nada.
    """
    bot = BotFalso(vivo=False)
    assert watchdog.pasada(bot, simular=False) == 1
    avisos = [m for m in entorno["mensajes"] if "no responde" in m.lower()]
    assert not avisos, "aviso al primer fallo"


def test_bot_caido_genera_alerta_tras_dos_fallos(entorno):
    bot = BotFalso(vivo=False)
    for _ in range(watchdog.FALLOS_ANTES_DE_AVISAR):
        watchdog.pasada(bot, simular=False)
    assert any("no responde" in m.lower() for m in entorno["mensajes"])


def test_el_contador_de_fallos_se_reinicia_al_recuperarse(entorno):
    """Un fallo, recuperacion, y otro fallo: no son dos seguidos.

    Sin reiniciar el contador, dos blips separados por horas de funcionamiento
    normal acabarian disparando la alerta.
    """
    bot = BotFalso(vivo=False)
    watchdog.pasada(bot, simular=False)

    bot.vivo = True
    watchdog.pasada(bot, simular=False)

    bot.vivo = False
    watchdog.pasada(bot, simular=False)

    avisos = [m for m in entorno["mensajes"] if "no responde" in m.lower()]
    assert not avisos, "aviso con fallos no consecutivos"


def test_bot_atascado_genera_alerta(entorno):
    """Responde a la API pero lleva mas de 10 minutos sin procesar una vela.

    Es el fallo mas traicionero: el proceso vive, el healthcheck del contenedor
    da verde, y el bot no esta gestionando las posiciones abiertas.
    """
    bot = BotFalso()
    bot.ultimo_ciclo = datetime.now(timezone.utc) - timedelta(
        minutes=watchdog.MINUTOS_SIN_LATIDO + 5)

    assert watchdog.pasada(bot, simular=False) == 1
    assert any("latido" in m.lower() for m in entorno["mensajes"])


def test_bot_con_latido_reciente_no_alerta(entorno):
    bot = BotFalso()
    bot.ultimo_ciclo = datetime.now(timezone.utc) - timedelta(minutes=1)

    watchdog.pasada(bot, simular=False)
    assert not any("latido" in m.lower() for m in entorno["mensajes"])


def test_alerta_de_bot_caido_no_se_repite_en_cada_pasada(entorno):
    """Seis pasadas con el bot caido generan una alerta, no seis."""
    bot = BotFalso(vivo=False)
    for _ in range(6):
        watchdog.pasada(bot, simular=False)

    avisos = [m for m in entorno["mensajes"] if "no responde" in m.lower()]
    assert len(avisos) == 1, f"se enviaron {len(avisos)} alertas de bot caido"


# ===========================================================================
# Coherencia entre capas
# ===========================================================================

def test_el_vigilante_usa_los_mismos_umbrales_que_la_estrategia():
    """El vigilante importa las reglas, no las copia.

    Dos copias de un umbral divergen siempre: alguien cambia una y olvida la
    otra, y el sistema queda con dos limites distintos segun quien pregunte.
    """
    import inspect

    fuente = inspect.getsource(watchdog)
    assert "from reglas_riesgo import" in fuente
    assert "0.03" not in fuente.replace("PERDIDA_DIARIA_MAXIMA", ""), \
        "hay un umbral diario codificado a mano en el vigilante"
    assert watchdog.PERDIDA_DIARIA_MAXIMA == PERDIDA_DIARIA_MAXIMA
    assert watchdog.DRAWDOWN_TOTAL_MAXIMO == DRAWDOWN_TOTAL_MAXIMO


def test_el_kill_switch_queda_enclavado(entorno):
    """Una vez disparado, no se vuelve a disparar en cada pasada.

    Redispararlo cursaria ordenes de cierre sobre una cuenta ya vacia y mandaria
    una alerta cada 5 minutos hasta que alguien silencie el chat. Ademas, el
    enclavamiento es la barrera que obliga a diagnosticar antes de rearmar.
    """
    bot = BotFalso(equity=10_000.0)
    watchdog.pasada(bot, simular=False)

    bot.equity = 8_500.0    # -15 % desde el pico
    for _ in range(6):
        watchdog.pasada(bot, simular=False)

    assert len(entorno["kill_switch"]) == 1, (
        f"el kill switch se disparo {len(entorno['kill_switch'])} veces"
    )
    avisos = [m for m in entorno["mensajes"] if "KILL SWITCH" in m]
    assert len(avisos) == 1, f"se enviaron {len(avisos)} alertas de kill switch"

    estado = json.loads(watchdog.ESTADO.read_text())
    assert estado["kill_switch_disparado"], "no quedo constancia del disparo"


def test_el_kill_switch_recibe_la_url_del_bot(entorno):
    """El vigilante le pasa la URL al kill switch, no asume 127.0.0.1.

    Cuando el vigilante corre en su propio contenedor —que es como se despliega
    en el docker-compose— `127.0.0.1` es el vigilante, no el bot. Sin la URL
    explicita, el kill switch fallaria exactamente en el momento en que hace
    falta que funcione.
    """
    bot = BotFalso(equity=10_000.0)
    bot.base_url = "http://freqtrade:8080"

    watchdog.pasada(bot, simular=False)
    bot.equity = 8_000.0
    watchdog.pasada(bot, simular=False)

    assert entorno["kill_switch"], "no se disparo el kill switch"
    comando = entorno["kill_switch"][0]
    assert "--url" in comando, "el kill switch se invoco sin --url"
    assert comando[comando.index("--url") + 1] == "http://freqtrade:8080"


def test_el_limite_diario_pausa_pero_no_detiene(entorno):
    """El limite diario usa `/pause`, nunca `/stop`.

    La diferencia no es de matiz. Con el bot en STOPPED, Freqtrade deja de
    procesar: nadie mueve el trailing ni ejecuta los stops de las posiciones ya
    abiertas. Un cortacircuitos que apaga la gestion del riesgo justo despues de
    un dia malo empeora exactamente la situacion que pretende contener.

    Se comprobo contra un bot real: con el trader detenido, la API ademas
    rechaza `forceexit` con "trader is not running".
    """
    bot = BotFalso(equity=10_000.0)
    watchdog.pasada(bot, simular=False)

    bot.equity = 9_600.0    # -4 %: supera el limite diario
    watchdog.pasada(bot, simular=False)

    assert bot.pausado, "no se pauso el bot al superar el limite diario"
    assert not bot.detenido, (
        "se uso /stop en vez de /pause: las posiciones abiertas quedarian "
        "sin gestionar"
    )
    assert "pausar" in bot.llamadas
    assert "detener" not in bot.llamadas
