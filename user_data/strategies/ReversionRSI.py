"""
ReversionRSI — comprar el retroceso profundo dentro de una tendencia alcista.

HIPOTESIS DE MERCADO
====================
Distinta de BaselineTrend, y a proposito casi opuesta.

BaselineTrend compra **continuacion**: entra cuando el impulso arranca y asume
que seguira. Esta compra **retroceso**: espera a que el precio caiga con fuerza
DENTRO de una tendencia que sigue intacta, y apuesta a que ese hueco se llena.

La idea de fondo es que en un mercado alcista los retrocesos profundos son
mayoritariamente liquidaciones y stops, no cambios de opinion sobre el valor.
Cuando el RSI se hunde por debajo de 30 y luego se recupera, la venta forzada
se agoto y quedan compradores esperando.

Por que puede funcionar donde la baseline no
--------------------------------------------
Son estrategias complementarias en el tiempo: BaselineTrend entra al principio
de un tramo alcista y esta se activa en medio, cuando la tendencia respira. Rara
vez se disparan a la vez, que es exactamente lo que se busca al correr varias.

EL FILTRO QUE LA HACE VIABLE
============================
La reversion a la media sin filtro de tendencia es la forma mas rapida de
arruinarse en cripto: cada rebote de un mercado bajista parece una oportunidad,
y "sobrevendido" puede seguir cayendo un 60 %. Por eso la condicion de regimen
—precio sobre la EMA(200)— no es opcional aqui, es la estrategia entera. Sin
ella esto es un cuchillo cayendo.
"""

from __future__ import annotations

import sys
from pathlib import Path

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import talib.abstract as ta
from pandas import DataFrame
from technical import qtpylib

from EstrategiaBase import EstrategiaBase
from reglas_riesgo import ATR_PERIODO

RSI_PERIODO = 14
RSI_SOBREVENTA = 30      # umbral de agotamiento vendedor
RSI_SALIDA = 65          # el retroceso se lleno
EMA_REGIMEN = 200
EMA_MEDIA = 50
VOLUMEN_SMA = 20


class ReversionRSI(EstrategiaBase):
    hipotesis = (
        "En una tendencia alcista intacta, los retrocesos profundos son "
        "liquidaciones y stops, no un cambio de opinion sobre el valor. Cuando "
        "el RSI sale de sobreventa, la venta forzada se agoto y el hueco tiende "
        "a llenarse."
    )

    startup_candle_count: int = 600

    plot_config = {
        "main_plot": {
            "ema_regimen": {"color": "#8395a7", "width": 2},
            "ema_media": {"color": "#2e86de"},
        },
        "subplots": {
            "RSI": {"rsi": {"color": "#5f27cd"}},
            "ATR": {"atr": {"color": "#ff9f43"}},
        },
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=RSI_PERIODO)
        dataframe["ema_regimen"] = ta.EMA(dataframe, timeperiod=EMA_REGIMEN)
        dataframe["ema_media"] = ta.EMA(dataframe, timeperiod=EMA_MEDIA)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=ATR_PERIODO)
        dataframe["volumen_sma"] = ta.SMA(dataframe["volume"], timeperiod=VOLUMEN_SMA)

        # Profundidad del retroceso respecto a la media de 50. Sirve para exigir
        # que el hueco sea de verdad y no un roce: un RSI bajo con el precio
        # pegado a la media no es un retroceso, es ruido lateral.
        dataframe["distancia_media"] = (
            (dataframe["close"] - dataframe["ema_media"]) / dataframe["ema_media"] * 100
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        entrada = (
            # 1. El RSI cruza al alza el umbral de sobreventa. Evento, no estado:
            #    se compra el momento en que la presion vendedora cede, no
            #    mientras dura — comprar "mientras esta sobrevendido" es comprar
            #    todo el camino hacia abajo.
            qtpylib.crossed_above(dataframe["rsi"], RSI_SOBREVENTA)
            # 2. La tendencia mayor sigue viva. SIN ESTO LA ESTRATEGIA ES UN
            #    CUCHILLO CAYENDO: en un mercado bajista, el RSI sale de
            #    sobreventa decenas de veces mientras el precio hace mínimos.
            & (dataframe["close"] > dataframe["ema_regimen"])
            # 3. El retroceso es profundo de verdad: al menos un 2 % por debajo
            #    de la media de 50.
            & (dataframe["distancia_media"] < -2.0)
            # 4. Participacion en el rebote.
            & (dataframe["volume"] > dataframe["volumen_sma"] * 0.8)
            & (dataframe["volume"] > 0)
            & dataframe["atr"].notna()
            & dataframe["ema_regimen"].notna()
        )
        dataframe.loc[entrada, ["enter_long", "enter_tag"]] = (1, "rebote_sobreventa")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """El hueco se lleno (RSI alto) o la tendencia se rompio."""
        salida = (
            (
                qtpylib.crossed_above(dataframe["rsi"], RSI_SALIDA)
                | (dataframe["close"] < dataframe["ema_regimen"])
            )
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[salida, ["exit_long", "exit_tag"]] = (1, "retroceso_llenado")
        return dataframe
