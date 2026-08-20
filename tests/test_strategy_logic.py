"""
T4 — Tests de la logica de senales.

Verifican que la estrategia dispara donde debe y, sobre todo, que NO dispara
donde no debe. Un test que solo comprueba "hay al menos una senal" no vale
nada: lo importante es que cada una de las 4 condiciones de entrada sea
realmente vinculante.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from conftest import (
    construir_ohlcv,
    indices_de_cruce,
    neutralizar_filtros,
    serie_con_cruce_alcista,
)


# ---------------------------------------------------------------------------
# Indicadores
# ---------------------------------------------------------------------------

def test_indicadores_se_calculan(estrategia, metadata):
    """populate_indicators produce todas las columnas que consumen las senales."""
    df = estrategia.populate_indicators(serie_con_cruce_alcista(), metadata)

    for columna in ("ema_rapida", "ema_lenta", "ema_regimen", "rsi", "atr", "volumen_sma"):
        assert columna in df.columns, f"falta la columna {columna}"
        assert df[columna].iloc[-1] == pytest.approx(df[columna].iloc[-1]), \
            f"{columna} termina en NaN"
        assert not pd.isna(df[columna].iloc[-1]), f"{columna} termina en NaN"


def test_emas_respetan_su_horizonte(estrategia, metadata):
    """En una tendencia alcista sostenida: ema_rapida > ema_lenta > ema_regimen.

    Es una comprobacion de cordura sobre los periodos: si alguien intercambiara
    por error los periodos 20 y 200, este test lo detecta.
    """
    precios = 100 * (1 + np.linspace(0, 0.5, 800))
    df = estrategia.populate_indicators(construir_ohlcv(precios), metadata)
    ultima = df.iloc[-1]

    assert ultima["ema_rapida"] > ultima["ema_lenta"] > ultima["ema_regimen"]


def test_atr_refleja_el_rango_real(estrategia, metadata):
    """Con rango constante conocido, el ATR converge a ese rango."""
    from conftest import construir_ohlcv_atr

    df = estrategia.populate_indicators(construir_ohlcv_atr(300, precio=100.0, rango=2.0),
                                        metadata)
    assert df["atr"].iloc[-1] == pytest.approx(2.0, rel=0.02)


# ---------------------------------------------------------------------------
# Senal de entrada: la vela correcta, y ni una antes
# ---------------------------------------------------------------------------

def test_entrada_solo_en_la_vela_del_cruce(estrategia, metadata):
    """La senal aparece exactamente en la vela del cruce, no antes ni despues.

    Este es EL test del ticket T4. Los otros tres filtros se neutralizan a
    proposito: aqui se prueba UNA cosa —el instante del disparo— y cada filtro
    tiene su propio test mas abajo. Un test que mezclara las cuatro condiciones
    no diria cual de ellas fallo cuando falle.
    """
    df = estrategia.populate_indicators(serie_con_cruce_alcista(), metadata)
    cruces = indices_de_cruce(df, alcista=True)
    assert cruces, "la serie sintetica no produjo ningun cruce alcista"

    df = estrategia.populate_entry_trend(neutralizar_filtros(df), metadata)
    senales = list(df.index[df["enter_long"] == 1])
    assert senales, "no se genero ninguna senal con los filtros neutralizados"

    # 1) Toda senal cae en una vela de cruce.
    for i in senales:
        assert i in cruces, (
            f"senal en la vela {i} ({df.loc[i, 'date']}) sin cruce de EMAs: "
            "la condicion se esta evaluando como estado y no como evento."
        )

    # 2) Ninguna senal en la vela ANTERIOR al cruce. Asi se manifestaria el
    #    lookahead en este punto: adelantar el disparo una vela.
    for i in cruces:
        if i > 0:
            assert df.loc[i - 1, "enter_long"] != 1, (
                f"senal en la vela {i - 1}, una antes del cruce en {i}: "
                "la estrategia esta anticipando informacion."
            )

    # 3) Ni en la posterior: el cruce genera su senal, no una con retraso.
    ultimo_cruce = max(cruces)
    assert ultimo_cruce in senales, (
        "el ultimo cruce alcista no genero senal con los filtros neutralizados"
    )


def test_entrada_es_evento_no_estado(estrategia, metadata):
    """En una tendencia alcista larga hay UNA senal, no una por vela.

    Si `crossed_above` se sustituyera por `>`, el bot intentaria entrar cada
    hora durante toda la tendencia. Este test lo impide.
    """
    precios = 100 * (1 + np.linspace(0, 0.8, 900))
    df = estrategia.populate_indicators(construir_ohlcv(precios), metadata)
    df = estrategia.populate_entry_trend(neutralizar_filtros(df), metadata)

    senales = int((df["enter_long"] == 1).sum())
    assert senales <= 3, (
        f"{senales} senales en una tendencia monotona. La condicion de cruce "
        "se esta evaluando como estado y no como evento."
    )


# ---------------------------------------------------------------------------
# Cada filtro tiene que ser vinculante
# ---------------------------------------------------------------------------

def test_filtro_regimen_bloquea_bajo_ema200(estrategia, metadata):
    """Un cruce alcista por debajo de la EMA(200) no genera entrada.

    Es el filtro que mas trabaja en la estrategia; si no fuera vinculante,
    el sistema compraria rebotes dentro de mercados bajistas.
    """
    df = neutralizar_filtros(
        estrategia.populate_indicators(serie_con_cruce_alcista(), metadata)
    )
    # Unica variable: el precio pasa a estar por debajo de la EMA(200).
    df["ema_regimen"] = df["close"] * 1.10
    df = estrategia.populate_entry_trend(df, metadata)

    assert "enter_long" not in df.columns or int((df["enter_long"] == 1).sum()) == 0, \
        "hubo entradas con el precio por debajo de la EMA(200)"


@pytest.mark.parametrize("rsi_forzado,debe_entrar", [
    (25.0, False),   # demasiado debil
    (39.9, False),   # justo por debajo del limite inferior
    (55.0, True),    # en la banda
    (70.1, False),   # justo por encima del limite superior
    (85.0, False),   # sobrecompra extrema
])
def test_banda_rsi_es_vinculante(estrategia, metadata, rsi_forzado, debe_entrar):
    """El RSI fuera de [40, 70] bloquea la entrada; dentro, la permite."""
    df = neutralizar_filtros(
        estrategia.populate_indicators(serie_con_cruce_alcista(), metadata)
    )
    df["rsi"] = rsi_forzado   # unica variable del experimento
    df = estrategia.populate_entry_trend(df, metadata)

    hubo = "enter_long" in df.columns and int((df["enter_long"] == 1).sum()) > 0
    assert hubo == debe_entrar, (
        f"con RSI={rsi_forzado} se esperaba entrada={debe_entrar} y hubo={hubo}"
    )


def test_filtro_volumen_bloquea_participacion_baja(estrategia, metadata):
    """Un cruce con volumen por debajo de su media de 20 no genera entrada."""
    df = neutralizar_filtros(
        estrategia.populate_indicators(serie_con_cruce_alcista(), metadata)
    )
    df["volumen_sma"] = df["volume"] * 2.0   # unica variable: volumen bajo
    df = estrategia.populate_entry_trend(df, metadata)

    assert "enter_long" not in df.columns or int((df["enter_long"] == 1).sum()) == 0, \
        "hubo entradas con volumen por debajo de la media"


def test_vela_sin_volumen_no_opera(estrategia, metadata):
    """Volumen 0 es un hueco de datos, no un mercado. No se opera sobre el."""
    df = neutralizar_filtros(
        estrategia.populate_indicators(serie_con_cruce_alcista(), metadata)
    )
    df["volume"] = 0.0
    df = estrategia.populate_entry_trend(df, metadata)

    assert "enter_long" not in df.columns or int((df["enter_long"] == 1).sum()) == 0


def test_indicadores_incompletos_no_generan_senal(estrategia, metadata):
    """Durante el calentamiento (indicadores en NaN) no puede haber senales."""
    df = estrategia.populate_indicators(serie_con_cruce_alcista(), metadata)
    df = estrategia.populate_entry_trend(df, metadata)

    if "enter_long" in df.columns:
        con_senal = df[df["enter_long"] == 1]
        assert not con_senal[["atr", "ema_regimen", "volumen_sma"]].isna().any().any(), \
            "se genero una senal sobre indicadores todavia en NaN"


# ---------------------------------------------------------------------------
# Senal de salida
# ---------------------------------------------------------------------------

def test_salida_en_cruce_bajista(estrategia, metadata):
    """La salida se marca en la vela donde EMA(20) cruza bajo EMA(50)."""
    # Subida larga y luego caida pronunciada.
    subida = 100 * (1 + np.linspace(0, 0.5, 700))
    caida = subida[-1] * (1 - np.linspace(0, 0.20, 120))
    df = construir_ohlcv(np.concatenate([subida, caida]))

    df = estrategia.populate_indicators(df, metadata)
    df = estrategia.populate_exit_trend(df, metadata)

    assert "exit_long" in df.columns
    salidas = df.index[df["exit_long"] == 1]
    assert len(salidas) > 0, "no se genero senal de salida en una caida clara"

    cruces_bajistas = indices_de_cruce(df, alcista=False)
    for i in salidas:
        assert i in cruces_bajistas, f"salida en {i} sin cruce bajista de EMAs"


def test_entrada_y_salida_no_coinciden(estrategia, metadata):
    """Ninguna vela puede llevar senal de entrada y de salida a la vez."""
    df = neutralizar_filtros(
        estrategia.populate_indicators(serie_con_cruce_alcista(), metadata)
    )
    df = estrategia.populate_entry_trend(df, metadata)
    df = estrategia.populate_exit_trend(df, metadata)

    if "enter_long" in df.columns and "exit_long" in df.columns:
        ambas = df[(df["enter_long"] == 1) & (df["exit_long"] == 1)]
        assert ambas.empty, f"{len(ambas)} velas con entrada y salida simultaneas"


# ---------------------------------------------------------------------------
# Configuracion de la estrategia
# ---------------------------------------------------------------------------

def test_configuracion_coherente_con_el_plan(estrategia):
    """Los ajustes estructurales de la estrategia son los del plan."""
    assert estrategia.timeframe == "1h"
    assert estrategia.can_short is False, "spot no permite cortos"
    assert estrategia.use_custom_stoploss is True
    assert estrategia.trailing_stop is False, (
        "el trailing nativo debe estar apagado: lo gestiona custom_stoploss en "
        "unidades de ATR, y dos mecanismos a la vez se pisan"
    )
    assert estrategia.process_only_new_candles is True
    assert estrategia.startup_candle_count >= 600, (
        "la EMA(200) necesita ~3x su periodo para converger; con menos, el "
        "backtest y la produccion no calculan el mismo filtro de regimen"
    )
    # minimal_roi efectivamente desactivado: un ROI fijo cortaria las
    # ganadoras largas, que son las que sostienen un sistema de tendencia.
    assert min(estrategia.minimal_roi.values()) >= 1.0
