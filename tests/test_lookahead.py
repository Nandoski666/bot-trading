"""
T4 — Test explicito de ausencia de sesgo de anticipacion.

`freqtrade lookahead-analysis` ya hace una comprobacion a nivel de sistema, pero
tarda minutos y necesita datos descargados. Estos tests son la version rapida y
quirurgica: verifican la propiedad matematica que define la ausencia de
lookahead, sobre datos sinteticos, en menos de un segundo.

**La propiedad:** el valor de un indicador y de una senal en la vela N debe
depender unicamente de las velas 0..N. Si se anaden velas despues de N, nada de
lo calculado en N puede cambiar.

Un indicador que la incumple —una media centrada, una normalizacion sobre el
maximo global, un `.shift(-1)` olvidado— cambia sus valores pasados al llegar
datos nuevos. Ese cambio es exactamente el futuro filtrandose hacia atras, y es
lo que hace que un backtest brillante pierda dinero en vivo.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from conftest import (
    DataProviderFalso,
    TradeFalso,
    WalletsFalsas,
    construir_ohlcv,
    construir_ohlcv_atr,
    neutralizar_filtros,
    serie_con_cruce_alcista,
)

INDICADORES = ["ema_rapida", "ema_lenta", "ema_regimen", "rsi", "atr", "volumen_sma"]


# ===========================================================================
# Los indicadores no cambian cuando llega el futuro
# ===========================================================================

def test_indicadores_no_cambian_al_anadir_velas_futuras(estrategia, metadata):
    """Truncar la serie no altera los valores ya calculados.

    Se calcula sobre la serie completa y sobre un prefijo. En el tramo comun,
    todos los indicadores tienen que coincidir bit a bit.
    """
    df_completo = serie_con_cruce_alcista()
    corte = len(df_completo) - 50

    ind_completo = estrategia.populate_indicators(df_completo.copy(), metadata)
    ind_truncado = estrategia.populate_indicators(
        df_completo.iloc[:corte].copy().reset_index(drop=True), metadata)

    for col in INDICADORES:
        a = ind_completo[col].iloc[:corte].to_numpy()
        b = ind_truncado[col].to_numpy()
        # Se comparan solo las posiciones donde ambos tienen valor: el prefijo
        # arranca con los mismos NaN de calentamiento en los dos casos.
        validos = ~(np.isnan(a) | np.isnan(b))
        assert validos.sum() > 100, f"muy pocos valores comparables en {col}"
        np.testing.assert_allclose(
            a[validos], b[validos], rtol=1e-9,
            err_msg=(f"'{col}' cambia de valor en el pasado al anadir velas futuras. "
                     "El indicador esta leyendo hacia adelante."),
        )


@pytest.mark.parametrize("velas_futuras", [1, 5, 25, 100])
def test_senales_no_cambian_al_anadir_velas_futuras(estrategia, metadata, velas_futuras):
    """Las senales de entrada y salida ya emitidas no se reescriben.

    Es la version de la propiedad anterior aplicada al resultado final. Aunque
    los indicadores fueran causales, un error en `populate_entry_trend` (por
    ejemplo comparar contra `df['close'].max()`) reintroduciria el sesgo aqui.
    """
    df_completo = serie_con_cruce_alcista()
    corte = len(df_completo) - velas_futuras

    def senales(df: pd.DataFrame) -> pd.DataFrame:
        d = neutralizar_filtros(estrategia.populate_indicators(df, metadata))
        d = estrategia.populate_entry_trend(d, metadata)
        d = estrategia.populate_exit_trend(d, metadata)
        for col in ("enter_long", "exit_long"):
            if col not in d.columns:
                d[col] = 0
        return d[["date", "enter_long", "exit_long"]].fillna(0)

    completo = senales(df_completo.copy())
    truncado = senales(df_completo.iloc[:corte].copy().reset_index(drop=True))

    pd.testing.assert_frame_equal(
        completo.iloc[:corte].reset_index(drop=True),
        truncado.reset_index(drop=True),
        check_dtype=False,
        obj=(f"senales del pasado tras anadir {velas_futuras} velas futuras"),
    )


def test_un_precio_futuro_extremo_no_altera_el_pasado(estrategia, metadata):
    """Prueba adversarial: se dispara la ultima vela un 500 %.

    Si algun calculo normalizara contra el maximo o el minimo de toda la serie
    —un error facil de cometer y dificil de ver— este cambio se propagaria
    hacia atras y el test lo detecta de inmediato.
    """
    df = serie_con_cruce_alcista()
    normal = estrategia.populate_indicators(df.copy(), metadata)

    manipulado = df.copy()
    for col in ("open", "high", "low", "close"):
        manipulado.loc[manipulado.index[-1], col] *= 6.0
    manipulado.loc[manipulado.index[-1], "volume"] *= 1000
    alterado = estrategia.populate_indicators(manipulado, metadata)

    for col in INDICADORES:
        a = normal[col].iloc[:-1].to_numpy()
        b = alterado[col].iloc[:-1].to_numpy()
        validos = ~(np.isnan(a) | np.isnan(b))
        np.testing.assert_allclose(
            a[validos], b[validos], rtol=1e-9,
            err_msg=(f"'{col}' cambio en el pasado al manipular SOLO la ultima vela. "
                     "Hay una dependencia del futuro."),
        )


def test_codigo_sin_desplazamientos_negativos():
    """Ninguna llamada a `.shift()` con argumento negativo en la estrategia.

    Es una comprobacion deliberadamente tonta y deliberadamente util:
    `.shift(-1)` es la forma mas directa de traerse el futuro, y se cuela con
    facilidad al copiar codigo de un notebook de analisis —donde mirar hacia
    adelante es legitimo— a una estrategia, donde no lo es.

    Se analiza el arbol sintactico y no el texto: buscar la cadena ".shift(-1)"
    daria positivo en cualquier comentario que la mencione, incluida la propia
    documentacion de este proyecto que explica por que no debe usarse.
    """
    import ast
    from pathlib import Path

    raiz = Path(__file__).resolve().parents[1] / "user_data" / "strategies"
    revisados = 0

    for archivo in raiz.glob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        revisados += 1

        # Unica excepcion: la etiqueta de entrenamiento de FreqAI, que ES el
        # retorno futuro por definicion. Acotada a ese metodo y verificada
        # aparte en tests/test_aprendizaje.py.
        permitidos = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.FunctionDef) and nodo.name == "set_freqai_targets":
                permitidos.update(range(nodo.lineno,
                                        (nodo.end_lineno or nodo.lineno) + 1))

        for nodo in ast.walk(arbol):
            if getattr(nodo, "lineno", None) in permitidos:
                continue
            if not (isinstance(nodo, ast.Call)
                    and isinstance(nodo.func, ast.Attribute)
                    and nodo.func.attr == "shift"):
                continue

            argumentos = list(nodo.args) + [kw.value for kw in nodo.keywords
                                            if kw.arg in (None, "periods")]
            for arg in argumentos:
                # -1 se parsea como UnaryOp(USub, Constant(1)).
                negativo = (
                    isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub)
                ) or (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, (int, float))
                    and arg.value < 0
                )
                assert not negativo, (
                    f"{archivo.name}:{nodo.lineno} llama a .shift() con un valor "
                    "negativo: eso desplaza datos del futuro hacia el presente."
                )

    assert revisados > 0, "no se reviso ningun archivo de estrategia"


# ===========================================================================
# Los callbacks leen solo velas cerradas
# ===========================================================================

def test_vela_cerrada_excluye_la_vela_en_curso(estrategia, metadata):
    """`_vela_cerrada_antes_de` nunca devuelve la vela del propio momento.

    Es la garantia central de los callbacks. En backtest el dataprovider expone
    la vela que se esta procesando; si se leyera con `.iloc[-1]`, el ATR del
    stop y el tamano de la posicion vendrian de una vela cuyo high, low y close
    todavia no habian ocurrido cuando se tomo la decision.
    """
    df = estrategia.populate_indicators(construir_ohlcv_atr(300, 1000.0, 20.0), metadata)
    estrategia.dp = DataProviderFalso(df)

    for pos in (-1, -2, -5, -50):
        momento = df["date"].iloc[pos]
        vela = estrategia._vela_cerrada_antes_de("BTC/USDT", momento)
        assert vela is not None
        assert vela["date"] < momento, (
            f"se devolvio una vela con fecha {vela['date']} para el momento {momento}"
        )
        # Y es concretamente la inmediatamente anterior, no una mas atras.
        esperada = df["date"].iloc[pos - 1]
        assert vela["date"] == esperada


def test_stake_ignora_la_vela_en_curso(estrategia, metadata):
    """El dimensionamiento no usa el ATR de la vela que aun esta ocurriendo.

    Se construye una serie tranquila cuya ULTIMA vela tiene un rango enorme. Si
    `custom_stake_amount` mirara esa vela, el ATR seria mayor y el stake, mucho
    menor. Al ignorarla, el stake debe ser el mismo que sin ella.
    """
    df = construir_ohlcv_atr(300, precio=1000.0, rango=20.0)

    # Vela en curso con un rango 20 veces mayor.
    en_curso = df.iloc[[-1]].copy()
    en_curso["date"] = en_curso["date"] + timedelta(hours=1)
    en_curso["high"] = 1000.0 + 200.0
    en_curso["low"] = 1000.0 - 200.0
    df_con_vela_viva = pd.concat([df, en_curso], ignore_index=True)

    momento = df_con_vela_viva["date"].iloc[-1]   # arranque de la vela en curso

    def stake_de(dataframe: pd.DataFrame) -> float:
        s = estrategia
        s.dp = DataProviderFalso(s.populate_indicators(dataframe.copy(), metadata))
        s.wallets = WalletsFalsas(10_000.0)
        return s.custom_stake_amount(
            pair="BTC/USDT", current_time=momento, current_rate=1000.0,
            proposed_stake=1000.0, min_stake=10.0, max_stake=10_000.0,
            leverage=1.0, entry_tag=None, side="long")

    sin_vela_viva = stake_de(df)
    con_vela_viva = stake_de(df_con_vela_viva)

    assert con_vela_viva == pytest.approx(sin_vela_viva, rel=1e-9), (
        "el tamano de la posicion cambio al anadir la vela en curso: "
        "custom_stake_amount la esta leyendo."
    )


def test_stop_ignora_la_vela_de_ejecucion(estrategia, metadata):
    """El ATR del stop viene de la vela de la SENAL, no de la de ejecucion.

    Secuencia real: la senal se genera al cerrar la vela N y la orden se llena
    en la apertura de la vela N+1. En ese instante, el rango de N+1 aun no
    existe. El stop tiene que calcularse con el ATR de N.
    """
    df = construir_ohlcv_atr(300, precio=1000.0, rango=20.0)

    # Vela de ejecucion, mucho mas volatil que el resto de la serie.
    ejecucion = df.iloc[[-1]].copy()
    ejecucion["date"] = ejecucion["date"] + timedelta(hours=1)
    ejecucion["high"] = 1000.0 + 150.0
    ejecucion["low"] = 1000.0 - 150.0
    df_total = pd.concat([df, ejecucion], ignore_index=True)

    ind = estrategia.populate_indicators(df_total, metadata)
    estrategia.dp = DataProviderFalso(ind)

    atr_senal = ind["atr"].iloc[-2]      # vela N — la unica disponible al decidir
    atr_ejecucion = ind["atr"].iloc[-1]  # vela N+1 — todavia en el futuro
    assert atr_ejecucion > atr_senal * 1.5, "el escenario de prueba no es discriminante"

    trade = TradeFalso(open_rate=1000.0, open_date_utc=ind["date"].iloc[-1])
    ratio = estrategia.custom_stoploss(
        pair="BTC/USDT", trade=trade, current_time=ind["date"].iloc[-1],
        current_rate=1000.0, current_profit=0.0, after_fill=True)

    stop = 1000.0 * (1 + ratio)
    assert stop == pytest.approx(1000.0 - 2.0 * atr_senal, rel=1e-6), (
        "el stop se calculo con el ATR de la vela de ejecucion, que en ese "
        "instante todavia no habia terminado de formarse."
    )
