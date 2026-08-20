"""
Reglas de riesgo del sistema — seccion 3 del plan.

Estas constantes son **hard-coded a proposito**. No son parametros de hyperopt,
no se leen de config.json y no se ajustan "para mejorar resultados". Viven en su
propio modulo para que cualquier cambio sea visible en el diff y para que los
tests puedan verificarlas de forma independiente de la estrategia.

Si alguna vez hay que cambiar una: se cambia aqui, se justifica por escrito en
docs/JOURNAL.md, y se vuelve a correr todo el proceso de validacion desde cero.
"""

from __future__ import annotations

# --- Riesgo por operacion --------------------------------------------------
# Fraccion del equity total que se pierde si el stop inicial se ejecuta.
# 0.5 % permite encadenar ~20 perdidas seguidas antes de tocar el limite de
# drawdown total. Con 2 % (la cifra que repite todo el mundo en internet) bastan
# 5 rachas malas para quedar fuera del juego.
RIESGO_POR_OPERACION = 0.005

# --- Exposicion simultanea -------------------------------------------------
# Con 3 pares correlacionados (BTC, ETH, SOL suelen moverse juntos) tres
# posiciones abiertas ya son, en la practica, una sola apuesta direccional
# apalancada 3x en riesgo. Mas de 3 seria enganarse.
MAX_POSICIONES_SIMULTANEAS = 3

# --- Cortacircuitos --------------------------------------------------------
# Perdida diaria: al superarse, el bot deja de ABRIR posiciones. Las abiertas se
# siguen gestionando (su stop sigue activo). Se reactiva solo a mano.
PERDIDA_DIARIA_MAXIMA = 0.03

# Drawdown total desde el maximo de equity: al superarse, kill switch — cierra
# todo y se apaga. Es el limite de "algo esta roto, o el mercado cambio, o la
# estrategia dejo de funcionar". En los tres casos la respuesta es parar.
DRAWDOWN_TOTAL_MAXIMO = 0.10

# --- Estructura del mercado ------------------------------------------------
APALANCAMIENTO = 1.0        # spot puro. 1.0 == sin apalancamiento.
PERMITE_CORTOS = False      # spot no permite vender lo que no se tiene.
PERMITE_PROMEDIAR_BAJA = False  # nunca. Promediar a la baja convierte una
                                # perdida acotada en una ilimitada.

# --- Parametros del stop (definidos en la estrategia, seccion 2 del plan) ---
ATR_PERIODO = 14
ATR_MULTIPLICADOR_STOP = 2.0        # stop inicial: entrada - 2 x ATR
ATR_ACTIVACION_TRAILING = 1.5       # el trailing arranca en +1.5 x ATR
ATR_DISTANCIA_TRAILING = 1.0        # y luego arrastra a 1 x ATR del maximo

# Backstop absoluto de Freqtrade. custom_stoploss siempre deberia dar un stop
# mas cercano que este; si por algun bug devolviera None, este valor evita que
# una posicion quede sin proteccion.
STOPLOSS_BACKSTOP = -0.15


def validar_reglas() -> None:
    """Comprobaciones de coherencia. Se ejecuta al arrancar la estrategia.

    No sustituye a los tests: es una red de seguridad para el caso en que
    alguien edite este archivo y arranque el bot sin correr pytest.
    """
    assert 0 < RIESGO_POR_OPERACION <= 0.01, (
        "El riesgo por operacion debe estar entre 0 % y 1 %. "
        f"Valor actual: {RIESGO_POR_OPERACION:.2%}"
    )
    assert 1 <= MAX_POSICIONES_SIMULTANEAS <= 3, (
        "Maximo 3 posiciones simultaneas (seccion 3 del plan). "
        f"Valor actual: {MAX_POSICIONES_SIMULTANEAS}"
    )
    assert RIESGO_POR_OPERACION * MAX_POSICIONES_SIMULTANEAS < PERDIDA_DIARIA_MAXIMA, (
        "El riesgo agregado de tener todas las posiciones abiertas y que todas "
        "toquen stop el mismo dia debe caber dentro del limite de perdida diaria."
    )
    assert PERDIDA_DIARIA_MAXIMA < DRAWDOWN_TOTAL_MAXIMO, (
        "El limite diario tiene que dispararse antes que el kill switch total."
    )
    assert APALANCAMIENTO == 1.0, "Solo spot. Sin apalancamiento."
    assert PERMITE_CORTOS is False, "Solo largos. Spot no permite cortos."
    assert PERMITE_PROMEDIAR_BAJA is False, "Prohibido promediar a la baja."
    assert ATR_ACTIVACION_TRAILING > ATR_DISTANCIA_TRAILING, (
        "El trailing debe activarse mas lejos de lo que arrastra; si no, "
        "al activarse cerraria la posicion de inmediato."
    )
