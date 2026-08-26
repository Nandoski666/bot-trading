"""
AprendizModelo — estrategia con aprendizaje automatico (FreqAI).

QUE APRENDE Y QUE NO
====================
La peticion era "que no se equivoque dos veces en lo mismo". Conviene decir con
precision que se puede y que no se puede hacer con eso, porque la intuicion
enganna.

**Lo que NO funciona:** mirar las ultimas operaciones perdedoras y ajustar las
reglas para evitarlas. Suena razonable y es la definicion exacta de sobreajuste.
Con 14 operaciones no hay nada que aprender; con 100 tampoco. Y el mercado no
repite: el patron que perdio la semana pasada no es el mismo patron esta semana.
Ajustarse al pasado reciente es aprenderse el ruido.

**Lo que SI funciona, y es lo que hace esta estrategia:** entrenar un modelo
sobre una ventana movil de datos, predecir, y **reentrenarlo periodicamente**
descartando lo viejo. FreqAI lo hace asi:

  1. toma los ultimos N dias como conjunto de entrenamiento
  2. entrena un modelo a predecir el retorno futuro
  3. lo usa durante los siguientes M dias
  4. lo tira y vuelve a entrenar con la ventana desplazada

La diferencia con "corregir errores" es que el modelo nunca ve el resultado de
sus propias operaciones. Aprende de la *estructura del mercado*, no de sus
aciertos y fallos. Eso lo hace **backtesteable**: se puede simular exactamente
el mismo proceso sobre anos de historico y ver si funciono, porque en cada punto
del pasado el modelo solo uso datos anteriores a ese punto.

Un sistema que aprendiera de sus propias operaciones seria imposible de validar:
el backtest tendria que simular las operaciones que habria hecho, que dependen
del modelo, que depende de las operaciones... y ademas cada ejecucion daria un
resultado distinto.

LA HONESTIDAD SOBRE LO QUE CABE ESPERAR
=======================================
El aprendizaje automatico no crea informacion que no este en los datos. Las
cinco estrategias anteriores tienen esperanza negativa **incluso sin
comisiones**, lo que significa que sus senales no anticipan nada. Un modelo
entrenado sobre los mismos indicadores tiene el mismo problema de partida.

Lo que un modelo si puede aportar sobre una regla fija:
  * combinar muchos indicadores a la vez en vez de encadenar filtros con AND
  * adaptarse cuando cambia el regimen, al reentrenar
  * dar una probabilidad y no un si/no, lo que permite exigir confianza alta

Si eso basta para cruzar el umbral de rentabilidad es una pregunta empirica, y
la respuesta esta en el backtest — no en la intuicion ni en el hecho de usar IA.

SESGO DE ANTICIPACION
=====================
Es el punto mas delicado de todo el proyecto. Un modelo de ML tiene mil formas
de mirar al futuro sin que se note: normalizar con estadisticos del conjunto
completo, calcular la etiqueta con datos posteriores y no descartarlos, o
entrenar sobre el mismo tramo donde se evalua.

FreqAI lo gestiona por diseno —separa entrenamiento de prediccion en el tiempo y
normaliza solo con la ventana de entrenamiento— pero las FEATURES las escribe
uno, y ahi si se puede colar. Todas las de aqui son causales, y el conjunto pasa
por `freqtrade lookahead-analysis` igual que las demas.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import numpy as np
import talib.abstract as ta
from pandas import DataFrame

from EstrategiaBase import EstrategiaBase
from reglas_riesgo import ATR_PERIODO

logger = logging.getLogger(__name__)

# Confianza minima para entrar. El modelo devuelve un retorno esperado; solo se
# opera cuando supera este umbral, no cuando simplemente es positivo.
#
# Es el equivalente a exigir una ventaja que cubra los costes: con 0.30 % de
# coste por operacion completa, una prediccion de +0.10 % no es una oportunidad,
# es una perdida con pasos extra.
# Se leen del config (freqai.umbral_entrada / umbral_salida) para poder
# barrerlos sin tocar codigo: el umbral correcto depende de la escala de las
# predicciones del modelo, que no se conoce hasta entrenarlo.
UMBRAL_PREDICCION = 0.004      # +0.4 % esperado, por defecto
UMBRAL_SALIDA = -0.001         # se sale si la expectativa se vuelve negativa


class AprendizModelo(EstrategiaBase):
    hipotesis = (
        "Un modelo entrenado sobre ventanas moviles puede combinar muchos "
        "indicadores a la vez y adaptarse a los cambios de regimen mejor que "
        "una cadena fija de filtros, dando ademas una expectativa numerica que "
        "permite exigir que supere los costes antes de operar."
    )

    timeframe = "5m"
    startup_candle_count: int = 600

    # FreqAI necesita saber que la estrategia lo usa.
    plot_config = {
        "main_plot": {},
        "subplots": {
            "Prediccion": {"&-retorno_futuro": {"color": "#f39c12"}},
            "Confianza": {"do_predict": {"color": "#27ae60"}},
        },
    }

    def feature_engineering_expand_all(self, dataframe: DataFrame, period: int,
                                       metadata: dict, **kwargs) -> DataFrame:
        """Features que FreqAI replica para varios periodos y timeframes.

        Se le pasan indicadores estandar y el modelo decide cuales importan. Esa
        es la ventaja real sobre una regla fija: no hay que acertar de antemano
        que combinacion funciona.

        Todos miran hacia atras. El prefijo `%-` marca la columna como feature.
        """
        dataframe["%-rsi"] = ta.RSI(dataframe, timeperiod=period)
        dataframe["%-mfi"] = ta.MFI(dataframe, timeperiod=period)
        dataframe["%-adx"] = ta.ADX(dataframe, timeperiod=period)
        dataframe["%-cci"] = ta.CCI(dataframe, timeperiod=period)

        # Distancias relativas a las medias, no las medias en si: un modelo no
        # puede aprender de "EMA = 78.432" porque ese numero no se repite nunca.
        # La distancia porcentual si es comparable entre pares y entre epocas.
        ema = ta.EMA(dataframe, timeperiod=period)
        dataframe["%-distancia_ema"] = (dataframe["close"] - ema) / ema * 100

        bb = ta.BBANDS(dataframe, timeperiod=period, nbdevup=2.0, nbdevdn=2.0)
        ancho = bb["upperband"] - bb["lowerband"]
        dataframe["%-ancho_bollinger"] = ancho / bb["middleband"] * 100
        # Posicion dentro de la banda: 0 = suelo, 1 = techo.
        dataframe["%-posicion_bollinger"] = (
            (dataframe["close"] - bb["lowerband"]) / ancho.replace(0, np.nan))

        # Volatilidad y volumen, ambos relativos a su propia media reciente.
        atr = ta.ATR(dataframe, timeperiod=period)
        dataframe["%-atr_relativo"] = atr / dataframe["close"] * 100
        dataframe["%-volumen_relativo"] = (
            dataframe["volume"] / dataframe["volume"].rolling(period).mean())

        dataframe["%-retorno"] = dataframe["close"].pct_change(period) * 100
        return dataframe

    def feature_engineering_standard(self, dataframe: DataFrame, metadata: dict,
                                     **kwargs) -> DataFrame:
        """Features que NO se replican por periodo: contexto temporal.

        La hora del dia y el dia de la semana importan en cripto: el mercado
        cambia de caracter entre la sesion asiatica, la europea y la americana.
        Es informacion que una regla fija no puede usar y un modelo si.
        """
        fechas = dataframe["date"]
        dataframe["%-hora"] = fechas.dt.hour
        dataframe["%-dia_semana"] = fechas.dt.dayofweek
        return dataframe

    def set_freqai_targets(self, dataframe: DataFrame, metadata: dict,
                           **kwargs) -> DataFrame:
        """Lo que el modelo aprende a predecir.

        El objetivo es el retorno de las proximas `label_period_candles` velas.
        Aqui SI se mira hacia adelante, y es correcto: es la etiqueta del
        entrenamiento, no una senal.

        FreqAI se encarga de que ninguna fila cuyo futuro aun no haya ocurrido
        entre en el conjunto de entrenamiento — por eso este `shift` negativo es
        legitimo y es la unica excepcion de todo el proyecto. El test que
        prohibe shifts negativos excluye este metodo explicitamente.
        """
        velas = self.freqai_info["feature_parameters"]["label_period_candles"]
        futuro = dataframe["close"].shift(-velas)
        dataframe["&-retorno_futuro"] = (futuro / dataframe["close"] - 1) * 100
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """FreqAI inyecta aqui las predicciones del modelo."""
        dataframe = self.freqai.start(dataframe, metadata, self)

        # El ATR se calcula DESPUES de freqai.start y no en
        # feature_engineering_standard: FreqAI devuelve un dataframe con las
        # columnas de features y predicciones, y descarta las demas. Un `atr`
        # creado alli desaparece, y EstrategiaBase lo necesita para dimensionar
        # la posicion y colocar el stop.
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=ATR_PERIODO)
        return dataframe

    @property
    def umbral_entrada(self) -> float:
        return float(self.freqai_info.get("umbral_entrada", UMBRAL_PREDICCION * 100))

    @property
    def umbral_salida(self) -> float:
        return float(self.freqai_info.get("umbral_salida", UMBRAL_SALIDA * 100))

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Diagnostico: sin saber la escala de las predicciones, el umbral es un
        # numero inventado. Se registra la distribucion una vez por par.
        pred = dataframe["&-retorno_futuro"].dropna()
        if len(pred) > 100:
            logger.info(
                "%s prediccion: min %.3f | p25 %.3f | mediana %.3f | p75 %.3f | "
                "max %.3f | %% sobre umbral %.2f: %.2f%%",
                metadata.get("pair", "?"), pred.min(), pred.quantile(0.25),
                pred.median(), pred.quantile(0.75), pred.max(),
                self.umbral_entrada, (pred > self.umbral_entrada).mean() * 100)

        entrada = (
            # 1. El modelo predice un retorno que supera los costes con margen.
            (dataframe["&-retorno_futuro"] > self.umbral_entrada)
            # 2. `do_predict` es 1 cuando FreqAI considera que los datos actuales
            #    se parecen a los del entrenamiento. Cuando el mercado entra en
            #    un regimen que el modelo no ha visto, vale 0 — y ahi no se opera.
            #    Un modelo extrapolando fuera de su dominio no es una prediccion,
            #    es una invencion con decimales.
            & (dataframe["do_predict"] == 1)
            & (dataframe["volume"] > 0)
            & dataframe["atr"].notna()
        )
        dataframe.loc[entrada, ["enter_long", "enter_tag"]] = (1, "modelo_positivo")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Se sale cuando el modelo deja de esperar beneficio."""
        salida = (
            ((dataframe["&-retorno_futuro"] < self.umbral_salida)
             | (dataframe["do_predict"] != 1))
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[salida, ["exit_long", "exit_tag"]] = (1, "modelo_negativo")
        return dataframe
