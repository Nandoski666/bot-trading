"""
BaselineTrendOpt — variante optimizable de BaselineTrend, solo para T6.

Por que existe un archivo aparte
--------------------------------
`BaselineTrend` no expone ningun parametro de hyperopt, y eso es deliberado:
es la linea base contra la que se mide todo lo demas. Una referencia que se
optimiza deja de ser una referencia.

Pero el walk-forward de T6 necesita algo que optimizar: su proposito es medir
**cuanto rendimiento se pierde** al pasar de los datos donde se ajustaron los
parametros a datos que el optimizador no vio. Sin optimizacion no hay
degradacion que medir.

De ahi la separacion:
  * `BaselineTrend`    — parametros fijos. Es lo que se opera.
  * `BaselineTrendOpt` — mismos indicadores, parametros abiertos. Es el
                         instrumento de medida del sobreajuste.

Que se puede optimizar y que no
-------------------------------
Solo los parametros de la SENAL: periodos de las EMAs rapida y lenta, y los
limites de la banda de RSI.

Las reglas de riesgo (0.5 % por operacion, 3 posiciones, 2 x ATR de stop,
umbrales de perdida diaria y drawdown) NO son optimizables y viven en
`reglas_riesgo.py`, heredadas sin tocar. La razon es concreta: un optimizador
que pueda mover el riesgo por operacion siempre descubrira que arriesgar mas
mejora el retorno del backtest, porque en el pasado ya sabemos que el sistema
no quebro. En vivo no existe esa garantia.

Tampoco se anaden indicadores nuevos. El espacio de busqueda se mantiene
pequeno a proposito: 4 parametros sobre ~2.500 combinaciones. Cuanto mayor es
el espacio, mas facil le resulta al optimizador encontrar ruido que parece
senal.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Freqtrade carga las estrategias por ruta, no como paquete. En el proceso
# principal eso basta, pero el hyperopt reparte el trabajo entre procesos hijo
# que vuelven a importar este archivo desde cero — y alli `from BaselineTrend
# import ...` fallaria porque el directorio no esta en sys.path. Anadirlo aqui
# hace que la herencia funcione tambien en los workers.
_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import talib.abstract as ta
from freqtrade.strategy import IntParameter
from pandas import DataFrame
from technical import qtpylib

from BaselineTrend import BaselineTrend, VOLUMEN_SMA
from reglas_riesgo import ATR_PERIODO


class BaselineTrendOpt(BaselineTrend):
    """Misma logica que BaselineTrend, con los parametros de senal abiertos."""

    # --- Espacio de busqueda (solo senal) ----------------------------------
    # Los rangos se eligen alrededor de valores convencionales, no para cubrir
    # todo lo posible. Un rango de 5 a 200 para la EMA rapida no explorararia
    # mas hipotesis: solo daria mas sitio donde encontrar coincidencias.
    ema_rapida = IntParameter(10, 30, default=20, space="buy", optimize=True)
    ema_lenta = IntParameter(40, 80, default=50, space="buy", optimize=True)
    rsi_minimo = IntParameter(30, 50, default=40, space="buy", optimize=True)
    rsi_maximo = IntParameter(60, 80, default=70, space="buy", optimize=True)

    # La EMA de regimen NO se optimiza. Es el filtro estructural de la
    # estrategia —"solo comprar en tendencia mayor alcista"— y no un dial que
    # se ajusta: moverla cambia la hipotesis de mercado, no su calibracion.
    ema_regimen_periodo = 200

    @property
    def combinacion_valida(self) -> bool:
        """Descarta combinaciones que no tienen sentido economico.

        La EMA rapida tiene que ser mas rapida que la lenta, y la banda de RSI
        tiene que tener anchura. El optimizador propone combinaciones al azar
        dentro de los rangos y algunas violan esto; se marcan como sin senales
        para que las descarte.
        """
        return (self.ema_rapida.value < self.ema_lenta.value
                and self.rsi_minimo.value < self.rsi_maximo.value - 5)

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema_rapida"] = ta.EMA(dataframe, timeperiod=self.ema_rapida.value)
        dataframe["ema_lenta"] = ta.EMA(dataframe, timeperiod=self.ema_lenta.value)
        dataframe["ema_regimen"] = ta.EMA(dataframe, timeperiod=self.ema_regimen_periodo)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=ATR_PERIODO)
        dataframe["volumen_sma"] = ta.SMA(dataframe["volume"], timeperiod=VOLUMEN_SMA)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        if not self.combinacion_valida:
            dataframe["enter_long"] = 0
            return dataframe

        senal = (
            qtpylib.crossed_above(dataframe["ema_rapida"], dataframe["ema_lenta"])
            & (dataframe["close"] > dataframe["ema_regimen"])
            & (dataframe["rsi"] > self.rsi_minimo.value)
            & (dataframe["rsi"] < self.rsi_maximo.value)
            & (dataframe["volume"] > dataframe["volumen_sma"])
            & (dataframe["volume"] > 0)
            & dataframe["atr"].notna()
            & dataframe["ema_regimen"].notna()
            & dataframe["volumen_sma"].notna()
        )
        dataframe.loc[senal, ["enter_long", "enter_tag"]] = (1, "cruce_ema_alcista")
        return dataframe
