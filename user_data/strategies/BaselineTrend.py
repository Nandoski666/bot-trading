"""
BaselineTrend — "Tendencia con filtro de regimen" (E0)
=======================================================

Esta NO es una estrategia ganadora. Es la linea base honesta contra la cual se
mide todo lo demas. Si una variante futura no le gana en metricas ajustadas por
riesgo, esa variante no vale la pena.

Universo   : BTC/USDT, ETH/USDT, SOL/USDT
Timeframe  : 1h
Direccion  : solo largos (spot, sin apalancamiento)

ENTRADA (todas las condiciones, sobre la vela CERRADA):
  1. EMA(20) cruza por encima de EMA(50)   -> arranque de impulso alcista
  2. close > EMA(200)                      -> filtro de regimen: solo compramos
                                              cuando la tendencia mayor acompana
  3. RSI(14) entre 40 y 70                 -> ni debilidad ni sobrecompra extrema
  4. volume > SMA(volume, 20)              -> el movimiento tiene participacion

SALIDA:
  - EMA(20) cruza por debajo de EMA(50), o
  - stop inicial (entrada - 2 x ATR14), o
  - trailing stop (se arma en +1.5 x ATR, luego arrastra a 1 x ATR del maximo)

RIESGO:
  - 0.5 % del equity por operacion, calculado desde la distancia real al stop
  - maximo 3 posiciones simultaneas
  - sin promediar a la baja, nunca

------------------------------------------------------------------------------
SESGO DE ANTICIPACION (lookahead bias) — leer antes de tocar nada
------------------------------------------------------------------------------
Es el error que convierte un backtest espectacular en una perdida real. Ocurre
cuando una decision usa informacion que en ese instante todavia no existia.

Como se evita aqui, punto por punto:

1. Freqtrade descarta la vela en formacion en Binance (`ohlcv_partial_candle`),
   asi que la ultima fila del dataframe es siempre una vela CERRADA. La senal se
   calcula sobre ella y la orden se ejecuta en la apertura de la vela siguiente.
   Esa es la secuencia real y es la que el backtester reproduce.

2. Todos los indicadores son causales: EMA, RSI, ATR y SMA solo miran hacia
   atras. No hay `.shift(-1)`, no hay `.rolling(...).mean()` centrado, no hay
   normalizaciones sobre el dataframe completo (dividir por `df['close'].max()`
   filtraria el futuro entero dentro de cada fila).

3. Los callbacks que corren en el momento de entrar (`custom_stake_amount`) o
   de gestionar el stop (`custom_stoploss`) NO leen `dataframe.iloc[-1]` a
   ciegas. Usan `_vela_cerrada_antes_de()`, que filtra explicitamente por
   `date < momento`. Sin ese filtro, en backtest se leeria la vela en curso
   —cuyo high/low/close aun no habian ocurrido— y el ATR del stop vendria del
   futuro.

4. El ATR que fija el stop se congela en la entrada (`trade.set_custom_data`).
   Recalcularlo en cada vela haria que el stop "supiera" la volatilidad
   posterior.

Verificacion automatica: `freqtrade lookahead-analysis` (ver `make lookahead`)
y `tests/test_lookahead.py`.
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

# --- Parametros de la senal (seccion 2 del plan) ---------------------------
# A diferencia de las reglas de riesgo, estos SI son candidatos legitimos a
# hyperopt. Se dejan como constantes con nombre para que el walk-forward sepa
# exactamente que puede tocar y que no.
EMA_RAPIDA = 20
EMA_LENTA = 50
EMA_REGIMEN = 200
RSI_PERIODO = 14
RSI_MINIMO = 40
RSI_MAXIMO = 70
VOLUMEN_SMA = 20


class BaselineTrend(EstrategiaBase):
    """Linea base: tendencia con filtro de regimen.

    Todo el riesgo —dimensionamiento, stop por ATR, trailing, limite de
    posiciones, protecciones— lo hereda de EstrategiaBase sin tocarlo. Aqui solo
    vive lo que distingue a esta estrategia de las demas: sus indicadores y sus
    senales.
    """

    hipotesis = (
        "Los precios de las criptomonedas grandes se mueven a rachas. Cuando "
        "arranca una racha alcista tiende a durar lo suficiente como para que "
        "entrar tarde y salir tarde deje margen, siempre que las salidas malas "
        "se corten pronto."
    )

    # Velas de calentamiento que Freqtrade descarta antes de permitir senales.
    #
    # El plan fijaba 200 (= periodo de la EMA mas larga). La medicion dice que
    # no basta: `freqtrade recursive-analysis` sobre BTC/USDT muestra que con
    # 200 velas la EMA(200) todavia se desvia **-0.63 %** de su valor
    # convergido, mientras que con 400+ el error cae a -0.014 %.
    #
    # Una EMA es un filtro de respuesta infinita: nunca olvida del todo su valor
    # inicial. Arrancarla con exactamente su periodo deja el 37 % del peso
    # contaminado por la semilla. Importa porque `close > ema_regimen` es el
    # filtro que mas operaciones descarta: con 200, el backtest y la produccion
    # no miden lo mismo.
    #
    # 600 = 3 x el periodo de la EMA mas larga. Coste: ~25 dias de datos
    # descartados al inicio de cada ventana.
    startup_candle_count: int = 600

    plot_config = {
        "main_plot": {
            "ema_rapida": {"color": "#2e86de"},
            "ema_lenta": {"color": "#ee5253"},
            "ema_regimen": {"color": "#8395a7", "width": 2},
        },
        "subplots": {
            "RSI": {"rsi": {"color": "#5f27cd"}},
            "ATR": {"atr": {"color": "#ff9f43"}},
        },
    }

    # ==================================================================
    # Indicadores
    # ==================================================================
    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Calcula los indicadores. Todos causales: solo miran hacia atras.

        Ninguna operacion aqui puede usar informacion posterior a la fila que
        esta rellenando. Eso descarta `.shift(-n)`, ventanas centradas y
        cualquier estadistico calculado sobre el dataframe entero.
        """
        # --- Tendencia: tres EMAs de horizonte creciente --------------------
        # 20 y 50 detectan el cambio de impulso; 200 define el regimen.
        dataframe["ema_rapida"] = ta.EMA(dataframe, timeperiod=EMA_RAPIDA)
        dataframe["ema_lenta"] = ta.EMA(dataframe, timeperiod=EMA_LENTA)
        dataframe["ema_regimen"] = ta.EMA(dataframe, timeperiod=EMA_REGIMEN)

        # --- Momento: RSI ---------------------------------------------------
        # Mide la fuerza relativa del movimiento reciente. Sirve para descartar
        # dos extremos: comprar algo sin fuerza (< 40) o justo en el clímax de
        # euforia (> 70), donde el recorrido restante suele ser poco y el
        # retroceso inmediato, probable.
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=RSI_PERIODO)

        # --- Volatilidad: ATR -----------------------------------------------
        # Rango verdadero medio. Es la unidad con la que medimos el riesgo: un
        # stop de "2 x ATR" se adapta solo a mercados tranquilos y agitados, a
        # diferencia de un stop porcentual fijo que en alta volatilidad salta
        # por ruido y en baja volatilidad arriesga de mas.
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=ATR_PERIODO)

        # --- Participacion: volumen vs. su media ----------------------------
        # Un cruce de medias con volumen por debajo de lo normal suele ser
        # deriva, no un movimiento con dinero detras.
        dataframe["volumen_sma"] = ta.SMA(dataframe["volume"], timeperiod=VOLUMEN_SMA)

        return dataframe

    # ==================================================================
    # Senal de entrada
    # ==================================================================
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Marca las velas en las que se cumplen las 4 condiciones de entrada.

        Todas se evaluan sobre la fila actual, que es una vela ya cerrada. La
        orden se envia al abrir la vela siguiente — asi lo simula el backtester
        y asi ocurre en vivo.
        """
        condiciones = [
            # 1. Cruce alcista de EMA(20) sobre EMA(50).
            #    `crossed_above` compara la fila actual con la anterior: es un
            #    evento puntual, no un estado. Solo dispara en la vela del cruce,
            #    lo que evita reentrar cada hora mientras dure la tendencia.
            qtpylib.crossed_above(dataframe["ema_rapida"], dataframe["ema_lenta"]),

            # 2. Filtro de regimen: por encima de la EMA(200).
            #    Es el filtro que mas trabaja. Elimina los cruces alcistas que
            #    ocurren dentro de un mercado bajista, que son la mayoria de las
            #    trampas en un sistema seguidor de tendencia.
            dataframe["close"] > dataframe["ema_regimen"],

            # 3. RSI en la banda 40-70.
            dataframe["rsi"] > RSI_MINIMO,
            dataframe["rsi"] < RSI_MAXIMO,

            # 4. Volumen por encima de su media de 20.
            dataframe["volume"] > dataframe["volumen_sma"],

            # Guarda tecnica: una vela con volumen 0 es un hueco de datos, no
            # un mercado. No se opera sobre ella.
            dataframe["volume"] > 0,

            # Guarda tecnica: durante las primeras ~200 velas los indicadores
            # aun son NaN. Freqtrade ya las descarta via startup_candle_count,
            # pero dejarlo explicito hace que los tests unitarios con
            # dataframes cortos se comporten igual que produccion.
            dataframe["atr"].notna(),
            dataframe["ema_regimen"].notna(),
            dataframe["volumen_sma"].notna(),
        ]

        # Combinar con AND lógico. `reduce` sobre `&` es equivalente pero esto
        # se lee mejor y produce el mismo resultado.
        senal = condiciones[0]
        for c in condiciones[1:]:
            senal = senal & c

        dataframe.loc[senal, ["enter_long", "enter_tag"]] = (1, "cruce_ema_alcista")
        return dataframe

    # ==================================================================
    # Senal de salida
    # ==================================================================
    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Salida por perdida de impulso: EMA(20) cruza por debajo de EMA(50).

        Las otras dos salidas (stop inicial y trailing) no viven aqui: las
        gestiona `custom_stoploss`, porque dependen del precio de entrada de
        cada posicion, no del estado del mercado.
        """
        salida = (
            qtpylib.crossed_below(dataframe["ema_rapida"], dataframe["ema_lenta"])
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[salida, ["exit_long", "exit_tag"]] = (1, "cruce_ema_bajista")
        return dataframe
