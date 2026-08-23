"""
MomentumMultiple — momento coherente en varios horizontes a la vez.

HIPOTESIS DE MERCADO
====================
El momento —que lo que ha subido siga subiendo— es de los pocos efectos que
sobreviven en muchos mercados y muchas decadas. Pero medido en un solo horizonte
es ruidoso: un +8 % en una semana puede ser tendencia o puede ser un unico dia
de euforia que ya se esta deshaciendo.

La hipotesis aqui es mas exigente: **el momento que es positivo en varios
horizontes a la vez es mas persistente que el momento de un solo horizonte.**
Que el precio este por encima de donde estaba hace 1 dia, hace 3 y hace 7
describe una subida sostenida; que solo lo este en uno describe un episodio.

Diferencia con BaselineTrend
----------------------------
La baseline dispara en el **evento** del cruce de medias: entra una vez, al
principio. Esta mide un **estado** —el momento coherente— y entra cuando ese
estado aparece, con un periodo de espera para no reentrar en bucle.

En la practica eso significa que entra mas tarde que la baseline pero tambien
en tendencias que ya estaban en marcha cuando el bot arranco, que la baseline
se pierde por completo. Con un bot recien puesto en produccion, esa diferencia
importa mas de lo que parece.
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

HORIZONTES = {"corto": 24, "medio": 72, "largo": 168}   # 1, 3 y 7 dias
MINIMO_LARGO = 2.0       # el horizonte largo debe superar el ruido
RSI_PERIODO = 14
RSI_MAXIMO = 72
EMA_REGIMEN = 200
VOLUMEN_SMA = 20
ESPERA_VELAS = 24        # no reentrar en el mismo estado durante 1 dia


class MomentumMultiple(EstrategiaBase):
    hipotesis = (
        "El momento positivo en varios horizontes a la vez (1, 3 y 7 dias) es "
        "mas persistente que el de un solo horizonte: describe una subida "
        "sostenida y no un episodio puntual que ya se esta deshaciendo."
    )

    startup_candle_count: int = 600

    plot_config = {
        "main_plot": {"ema_regimen": {"color": "#8395a7", "width": 2}},
        "subplots": {
            "Momento": {
                "mom_corto": {"color": "#2e86de"},
                "mom_medio": {"color": "#f39c12"},
                "mom_largo": {"color": "#27ae60"},
            },
        },
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # Retorno porcentual sobre cada horizonte. `pct_change(n)` compara con la
        # vela de hace n periodos: mira hacia atras, nunca hacia adelante.
        for nombre, velas in HORIZONTES.items():
            dataframe[f"mom_{nombre}"] = dataframe["close"].pct_change(velas) * 100

        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=RSI_PERIODO)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=ATR_PERIODO)
        dataframe["ema_regimen"] = ta.EMA(dataframe, timeperiod=EMA_REGIMEN)
        dataframe["volumen_sma"] = ta.SMA(dataframe["volume"], timeperiod=VOLUMEN_SMA)

        # Estado "momento coherente": positivo en los tres horizontes.
        dataframe["momento_coherente"] = (
            (dataframe["mom_corto"] > 0)
            & (dataframe["mom_medio"] > 0)
            & (dataframe["mom_largo"] > MINIMO_LARGO)
        )
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # El estado acaba de aparecer, o lleva activo un tiempo pero no se ha
        # entrado en las ultimas ESPERA_VELAS. `rolling(...).max()` sobre el
        # estado desplazado dice si ya estaba activo hace poco: sin esto, un
        # estado que dura semanas generaria una senal cada hora.
        recien = ~dataframe["momento_coherente"].shift(1).fillna(False)
        sin_senal_reciente = (
            dataframe["momento_coherente"]
            .shift(1)
            .fillna(False)
            .rolling(ESPERA_VELAS)
            .max()
            .fillna(0)
            == 0
        )

        entrada = (
            dataframe["momento_coherente"]
            & (recien | sin_senal_reciente)
            & (dataframe["close"] > dataframe["ema_regimen"])
            # Sin clímax: por encima de 72 el recorrido restante suele ser poco
            # y el retroceso inmediato, probable.
            & (dataframe["rsi"] < RSI_MAXIMO)
            & (dataframe["volume"] > 0)
            & dataframe["atr"].notna()
            & dataframe["mom_largo"].notna()
            & dataframe["ema_regimen"].notna()
        )
        dataframe.loc[entrada, ["enter_long", "enter_tag"]] = (1, "momento_coherente")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """El momento dejo de ser coherente: se rompio la tesis."""
        salida = (
            (dataframe["mom_corto"] < 0)
            & (dataframe["mom_medio"] < 0)
            & (dataframe["volume"] > 0)
        )
        dataframe.loc[salida, ["exit_long", "exit_tag"]] = (1, "momento_roto")
        return dataframe
