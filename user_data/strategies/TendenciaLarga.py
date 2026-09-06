"""
TendenciaLarga — seguimiento de tendencia en velas diarias.

POR QUE ESTA ES DISTINTA DE LAS SEIS ANTERIORES
================================================
Las seis estrategias previas fallaron todas por lo mismo, y esta ataca esa
causa concreta en vez de proponer otra combinacion de indicadores.

El dato que lo explica todo, medido sobre BTC/USDT:

    movimiento tipico de una vela        coste ida y vuelta      ratio
    5 minutos:  ~0.5 %                   0.30 %                  ~60 %
    1 dia:      ~4.4 % (ATR)             0.30 %                  ~6.8 %

En velas de 5 minutos el coste se come el 60 % del movimiento disponible. La
estrategia tiene que acertar muchisimo solo para empatar. En velas diarias el
mismo coste representa el 6.8 %: sigue existiendo, pero deja de ser el factor
dominante y pasa a ser un peaje asumible.

**Nueve veces mejor relacion senal/coste.** Ese es el cambio, y no es una
opinion sobre el mercado: es aritmetica sobre datos propios.

LA HIPOTESIS
============
Seguimiento de tendencia clasico, el metodo con mas evidencia documentada fuera
de muestra que existe en gestion sistematica — decadas de resultados publicos en
futuros, divisas y materias primas, y especialmente aplicable a cripto porque
cripto tiende con violencia.

La idea es vieja y aburrida a proposito:

  * comprar cuando el precio supera su maximo de N dias
  * dejar correr la posicion mientras la tendencia dure
  * salir cuando pierde su minimo de M dias

No predice nada. Asume que las tendencias largas existen, que no se sabe cuando
empiezan ni cuando acaban, y que la unica forma de capturarlas es entrar tarde,
salir tarde, y aguantar el ruido de en medio.

QUE CAMBIA RESPECTO A LAS ANTERIORES
====================================
1. **Frecuencia**: de ~4.400 operaciones al ano a unas decenas. El coste total
   pagado en comisiones cae en dos ordenes de magnitud.

2. **Duracion**: de 30 minutos a semanas o meses. Una operacion ganadora tiene
   espacio para capturar un movimiento grande, no un 1 %.

3. **Trailing ancho**: 3 x ATR diario en vez de 1 x. Con ATR diario del 4.4 %,
   un trailing de 1 x ATR cerraria en cualquier retroceso normal y haria
   imposible seguir una tendencia. Es el error que arruina a la mayoria de
   seguidores de tendencia mal calibrados.

4. **Asimetria**: se espera un win rate BAJO —30-40 %— con ganadoras mucho
   mayores que las perdedoras. Si el win rate sale alto, algo va mal: significa
   que se estan cortando las ganadoras.

LO QUE NO GARANTIZA
===================
Que funcione. El seguimiento de tendencia pasa por travesias del desierto de
anos, y los ultimos anos de cripto pueden ser una de ellas. La unica forma de
saberlo es el backtest sobre el historico completo, con costes, y compararlo
contra comprar y mantener BTC — que es el liston real, y que gano a las seis
estrategias anteriores.
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

# --- Parametros del canal ----------------------------------------------------
# 55 dias de entrada y 20 de salida son los numeros del sistema Turtle, elegidos
# a proposito por ser publicos y no ajustados a estos datos. Cualquier valor que
# "funcione mejor" en este historico concreto seria sospechoso de sobreajuste.
CANAL_ENTRADA = 55
CANAL_SALIDA = 20

# Filtro de regimen: la media de 200 dias separa mercados alcistas de bajistas.
EMA_REGIMEN = 200

# Volumen minimo relativo, para no entrar en rupturas sin participacion.
VOLUMEN_SMA = 20


class TendenciaLarga(EstrategiaBase):
    hipotesis = (
        "Las tendencias largas existen y no se puede saber cuando empiezan ni "
        "cuando terminan. La unica forma de capturarlas es entrar tarde al "
        "superar un maximo de 55 dias, aguantar el ruido con un stop ancho, y "
        "salir tarde al perder el minimo de 20 dias. En velas diarias el coste "
        "de transaccion pasa de ser el 60 % del movimiento disponible a menos "
        "del 7 %, que es lo que hace viable la operacion."
    )

    timeframe = "1d"

    # 200 velas diarias para la EMA de regimen, x3 para que converja de verdad.
    # Son ~600 dias: hay 2.100 disponibles, asi que sobra.
    startup_candle_count: int = 600

    # --- Trailing ancho ------------------------------------------------------
    # Con ATR diario del ~4.4 %, un trailing de 1 x ATR cierra en cualquier
    # retroceso normal. 3 x ATR (~13 %) deja respirar a la tendencia.
    #
    # El stop INICIAL sigue siendo 2 x ATR y el riesgo sigue siendo 0.5 %: eso
    # no se toca. Lo unico que cambia es cuando se recoge el beneficio.
    atr_activacion_trailing: float = 4.0
    atr_distancia_trailing: float = 3.0

    # Una senal diaria caduca en 1 vela: si no se ejecuto hoy, manana es otra
    # situacion de mercado.
    ignore_buying_expired_candle_after = 86400

    plot_config = {
        "main_plot": {
            "canal_alto": {"color": "#27ae60"},
            "canal_bajo": {"color": "#c0392b"},
            "ema_regimen": {"color": "#8395a7", "width": 2},
        },
        "subplots": {"ATR": {"atr": {"color": "#ff9f43"}}},
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Canal de Donchian. El `shift(1)` es imprescindible: sin el, "close >
        # maximo de los ultimos 55 dias" se compara con un maximo que incluye la
        # propia vela de hoy, y seria cierto siempre que hoy marque maximo.
        dataframe["canal_alto"] = dataframe["high"].rolling(CANAL_ENTRADA).max().shift(1)
        dataframe["canal_bajo"] = dataframe["low"].rolling(CANAL_SALIDA).min().shift(1)

        dataframe["ema_regimen"] = ta.EMA(dataframe, timeperiod=EMA_REGIMEN)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=ATR_PERIODO)
        dataframe["volumen_sma"] = ta.SMA(dataframe["volume"], timeperiod=VOLUMEN_SMA)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        entrada = (
            # 1. Ruptura del maximo de 55 dias.
            (dataframe["close"] > dataframe["canal_alto"])
            # 2. Evento y no estado: ayer estaba dentro del canal.
            & (dataframe["close"].shift(1) <= dataframe["canal_alto"].shift(1))
            # 3. Regimen alcista. En cripto los mercados bajistas producen
            #    rupturas al alza constantemente, y casi todas fallan.
            & (dataframe["close"] > dataframe["ema_regimen"])
            # 4. Participacion. Una ruptura diaria sin volumen suele ser un
            #    barrido que se deshace en dias.
            & (dataframe["volume"] > dataframe["volumen_sma"])
            & (dataframe["volume"] > 0)
            & dataframe["atr"].notna()
            & dataframe["canal_alto"].notna()
            & dataframe["ema_regimen"].notna()
        )
        dataframe.loc[entrada, ["enter_long", "enter_tag"]] = (1, "ruptura_55d")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Salida por perdida del minimo de 20 dias.

        Es deliberadamente lenta. Un seguidor de tendencia devuelve siempre una
        parte del beneficio al salir — es el precio de no adivinar el techo. Una
        salida rapida sube el win rate y destruye el sistema, porque corta las
        pocas operaciones grandes que pagan a todas las demas.
        """
        salida = (
            (dataframe["close"] < dataframe["canal_bajo"])
            & (dataframe["volume"] > 0)
            & dataframe["canal_bajo"].notna()
        )
        dataframe.loc[salida, ["exit_long", "exit_tag"]] = (1, "perdida_canal_20d")
        return dataframe
