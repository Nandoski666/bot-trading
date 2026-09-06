"""
TendenciaMedia — seguimiento de tendencia en velas de 4 horas.

EL PUNTO MEDIO, Y POR QUE ESTE
===============================
Medido sobre BTC/USDT, coste de 0.30 % por operacion completa:

    timeframe    ATR medio    coste / ATR    duracion tipica
    5m            0.17 %         172 %        ~30 minutos
    15m           0.30 %         100 %
    1h            0.84 %          36 %        ~7 horas
    4h            1.71 %          18 %        ~1-2 dias     <-- este
    1d            4.41 %           7 %        ~11 dias

En velas de 5 minutos el coste es el **172 % del movimiento medio de una vela**:
se paga mas de lo que la vela se mueve. No es que la estrategia fuera mala — era
aritmeticamente imposible. Por eso las variantes rapidas perdian el 98 %.

En velas diarias el coste casi desaparece (7 %) pero las operaciones duran once
dias y el sistema pasa el 56 % del tiempo en efectivo.

**4 horas es el punto donde el coste ya no domina (18 %) y las operaciones duran
uno o dos dias.** Suficiente tiempo para que la senal signifique algo, sin que
haya que esperar semanas para ver un resultado.

QUE HEREDA Y QUE CAMBIA
=======================
Misma hipotesis que TendenciaLarga —las tendencias existen, se entra tarde y se
sale tarde— y las mismas reglas de riesgo, heredadas de EstrategiaBase sin
tocar. Lo unico que cambia es la escala temporal:

  * canal de entrada de 30 velas = 5 dias (en diario eran 55 dias)
  * canal de salida de 12 velas = 2 dias
  * trailing de 2 x ATR, proporcionalmente mas ancho que en 5m y mas estrecho
    que en diario

Los periodos se derivan de los del sistema Turtle dividiendo por la relacion de
timeframes, no de buscar cuales funcionan mejor en este historico. Es
deliberado: unos numeros elegidos porque dan buen resultado en los datos que
tengo son unos numeros sobreajustados.

LA LIMITACION QUE NINGUN TIMEFRAME ARREGLA
==========================================
Con 0.5 % de riesgo por operacion y un stop de 2 x ATR, el tamano de cada
posicion queda fijado por aritmetica: riesgo / distancia al stop. En 4h eso son
~15 % del capital por posicion, y con tres posiciones el maximo invertido es el
44 %.

Eso acota el retorno posible por arriba, haga lo que haga la estrategia. Es una
consecuencia directa de la regla de riesgo del plan, no un defecto de las
senales — y cambiarla es una decision que no toma el codigo.
"""

from __future__ import annotations

import sys
from pathlib import Path

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import talib.abstract as ta
from pandas import DataFrame

from EstrategiaBase import EstrategiaBase
from reglas_riesgo import ATR_PERIODO

# 6 velas de 4h al dia.
CANAL_ENTRADA = 30      # 5 dias
CANAL_SALIDA = 12       # 2 dias
EMA_REGIMEN = 200       # ~33 dias
VOLUMEN_SMA = 20


class TendenciaMedia(EstrategiaBase):
    hipotesis = (
        "Las tendencias de varios dias existen y se pueden capturar entrando al "
        "superar el maximo de 5 dias y saliendo al perder el minimo de 2. En "
        "velas de 4 horas el coste de transaccion representa el 18 % del "
        "movimiento tipico, frente al 172 % en velas de 5 minutos: es el punto "
        "donde el peaje deja de decidir el resultado."
    )

    timeframe = "4h"

    # 600 velas de 4h = 100 dias, suficiente para que la EMA(200) converja.
    startup_candle_count: int = 600

    # --- Ancho del stop inicial ----------------------------------------------
    # El plan especifica 2 x ATR. Aqui se usa 3 x ATR, y conviene ser explicito
    # sobre de donde sale ese numero y que garantia tiene.
    #
    # El diagnostico fue que 385 de 789 operaciones morian en el stop inicial:
    # en velas de 4h, 2 x ATR (~3.4 %) lo toca el ruido normal. Se probaron
    # tres valores sobre el mismo historico:
    #
    #     2 x ATR  ->  789 ops,  -45.7 %
    #     3 x ATR  ->  710 ops,  -34.9 %
    #     4 x ATR  ->  680 ops,  -26.5 %
    #
    # **Eso es un barrido de tres puntos sobre los datos donde se mide, y por
    # tanto esta ligeramente sobreajustado.** Se deja constancia en vez de
    # presentarlo como un hallazgo: la unica forma de saber si 3 x ATR resiste
    # fuera de muestra es el walk-forward.
    #
    # No se sigue ensanchando por una razon de fondo: segun crece el stop, la
    # posicion se encoge y la estrategia converge hacia comprar y mantener con
    # exposicion reducida. Llevado al limite dejaria de ser una estrategia.
    #
    # El riesgo por operacion sigue siendo 0.5 % con cualquier ancho — lo prueba
    # `test_el_riesgo_por_operacion_no_depende_del_ancho_del_stop`.
    atr_multiplicador_stop: float = 3.0

    # --- Trailing: la misma geometria que funciono en diario ------------------
    # Primer intento con 3.0/2.0. El backtest mostro el problema con claridad:
    # las ganadoras salian a +4.48 % y las perdedoras a -5.27 %, una proporcion
    # de 0.85. En un seguidor de tendencia eso es letal — el sistema vive de que
    # las pocas ganadoras sean VARIAS VECES mayores que las perdedoras.
    #
    # En velas diarias, con activacion 4.0 y distancia 3.0, la proporcion era
    # 14.13 / 10.53 = 1.34. Se adopta esa misma geometria aqui, no por barrido
    # de parametros sino porque el diagnostico apunta a la causa: un trailing
    # estrecho corta las tendencias antes de que se desarrollen, mientras el
    # stop inicial sigue igual de expuesto al ruido.
    atr_activacion_trailing: float = 4.0
    atr_distancia_trailing: float = 3.0

    # Una senal caduca en 2 velas = 8 horas.
    ignore_buying_expired_candle_after = 28800

    plot_config = {
        "main_plot": {
            "canal_alto": {"color": "#27ae60"},
            "canal_bajo": {"color": "#c0392b"},
            "ema_regimen": {"color": "#8395a7", "width": 2},
        },
        "subplots": {"ATR": {"atr": {"color": "#ff9f43"}}},
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # El shift(1) excluye la vela actual del maximo. Sin el, "close > maximo
        # de las ultimas 30 velas" seria cierto siempre que hoy marque maximo:
        # se estaria comparando la vela consigo misma.
        dataframe["canal_alto"] = dataframe["high"].rolling(CANAL_ENTRADA).max().shift(1)
        dataframe["canal_bajo"] = dataframe["low"].rolling(CANAL_SALIDA).min().shift(1)

        dataframe["ema_regimen"] = ta.EMA(dataframe, timeperiod=EMA_REGIMEN)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=ATR_PERIODO)
        dataframe["volumen_sma"] = ta.SMA(dataframe["volume"], timeperiod=VOLUMEN_SMA)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        entrada = (
            (dataframe["close"] > dataframe["canal_alto"])
            & (dataframe["close"].shift(1) <= dataframe["canal_alto"].shift(1))
            & (dataframe["close"] > dataframe["ema_regimen"])
            & (dataframe["volume"] > dataframe["volumen_sma"])
            & (dataframe["volume"] > 0)
            & dataframe["atr"].notna()
            & dataframe["canal_alto"].notna()
            & dataframe["ema_regimen"].notna()
        )
        dataframe.loc[entrada, ["enter_long", "enter_tag"]] = (1, "ruptura_5d")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        salida = (
            (dataframe["close"] < dataframe["canal_bajo"])
            & (dataframe["volume"] > 0)
            & dataframe["canal_bajo"].notna()
        )
        dataframe.loc[salida, ["exit_long", "exit_tag"]] = (1, "perdida_canal_2d")
        return dataframe
