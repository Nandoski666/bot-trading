"""
RupturaDonchian — ruptura de rango tras un periodo de compresion.

HIPOTESIS DE MERCADO
====================
La volatilidad no es constante: se comprime y se expande, y esos dos estados se
alternan. Un mercado que lleva dias moviendose en un rango estrecho esta
acumulando ordenes a ambos lados; cuando el precio sale de ese rango, las
ordenes en espera se ejecutan y el movimiento se alimenta a si mismo.

Es distinta de las otras tres:
  * BaselineTrend compra el arranque de un impulso ya visible
  * ReversionRSI compra el retroceso dentro de la tendencia
  * Orochi compra el rechazo de precios fuera del valor
  * esta compra **la salida de un rango**, que es un cuarto momento del ciclo

Las dos condiciones son inseparables
------------------------------------
Una ruptura sin compresion previa no dice nada: en un mercado que ya se mueve
mucho, hacer un maximo de 48 horas es rutina. Lo que da informacion es la
ruptura que llega **despues de un periodo anormalmente tranquilo**, porque solo
entonces significa que algo cambio.

Por eso se exige que el ATR relativo este en el tercio bajo de su propio
historial reciente antes de la ruptura. Es el filtro que separa esta estrategia
de "comprar maximos", que es una forma conocida de perder dinero.
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

CANAL_ENTRADA = 48        # velas: maximo de 2 dias
CANAL_SALIDA = 24         # velas: minimo de 1 dia
VENTANA_COMPRESION = 336  # 2 semanas de referencia para juzgar la volatilidad
PERCENTIL_COMPRESION = 0.40
EMA_REGIMEN = 200
VOLUMEN_SMA = 20
FACTOR_VOLUMEN = 1.3      # la ruptura necesita mas volumen del habitual


class RupturaDonchian(EstrategiaBase):
    hipotesis = (
        "La volatilidad se comprime y se expande en ciclos. Un rango estrecho "
        "acumula ordenes a ambos lados; al romperlo, esas ordenes se ejecutan y "
        "el movimiento se realimenta. La compresion previa es lo que distingue "
        "una ruptura informativa de un maximo rutinario."
    )

    startup_candle_count: int = 600

    plot_config = {
        "main_plot": {
            "canal_alto": {"color": "#27ae60"},
            "canal_bajo": {"color": "#c0392b"},
            "ema_regimen": {"color": "#8395a7", "width": 2},
        },
        "subplots": {"Compresion": {"atr_relativo": {"color": "#ff9f43"}}},
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # --- Canal de Donchian ------------------------------------------------
        # El maximo se calcula EXCLUYENDO la vela actual (shift(1)). Sin ese
        # desplazamiento, "close > maximo de las ultimas 48" seria trivialmente
        # cierto siempre que la vela actual marque el maximo — se estaria
        # comparando la vela consigo misma.
        dataframe["canal_alto"] = dataframe["high"].rolling(CANAL_ENTRADA).max().shift(1)
        dataframe["canal_bajo"] = dataframe["low"].rolling(CANAL_SALIDA).min().shift(1)

        # --- Compresion de volatilidad ----------------------------------------
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=ATR_PERIODO)
        dataframe["atr_relativo"] = dataframe["atr"] / dataframe["close"] * 100

        # Percentil del ATR relativo dentro de su propia ventana movil. Se usa
        # una ventana movil y NO el historico completo: calcular el percentil
        # sobre toda la serie meteria en cada fila informacion de todo el
        # futuro, que es sesgo de anticipacion del mas puro.
        dataframe["percentil_atr"] = (
            dataframe["atr_relativo"]
            .rolling(VENTANA_COMPRESION)
            .rank(pct=True)
            .shift(1)
        )

        dataframe["ema_regimen"] = ta.EMA(dataframe, timeperiod=EMA_REGIMEN)
        dataframe["volumen_sma"] = ta.SMA(dataframe["volume"], timeperiod=VOLUMEN_SMA)
        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        entrada = (
            # 1. Ruptura del canal superior.
            (dataframe["close"] > dataframe["canal_alto"])
            # 2. Que sea el evento y no el estado: la vela anterior estaba dentro.
            & (dataframe["close"].shift(1) <= dataframe["canal_alto"].shift(1))
            # 3. Venia de compresion. Sin esto, esto seria "comprar maximos".
            & (dataframe["percentil_atr"] < PERCENTIL_COMPRESION)
            # 4. Regimen alcista: no se compran rupturas dentro de un mercado
            #    bajista, donde suelen ser el ultimo suspiro de un rebote.
            & (dataframe["close"] > dataframe["ema_regimen"])
            # 5. Volumen por encima de lo normal. Una ruptura sin volumen suele
            #    ser un barrido de stops que se deshace en las horas siguientes.
            & (dataframe["volume"] > dataframe["volumen_sma"] * FACTOR_VOLUMEN)
            & (dataframe["volume"] > 0)
            & dataframe["atr"].notna()
            & dataframe["canal_alto"].notna()
            & dataframe["percentil_atr"].notna()
            & dataframe["ema_regimen"].notna()
        )
        dataframe.loc[entrada, ["enter_long", "enter_tag"]] = (1, "ruptura_comprimida")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Salida por canal inferior: la ruptura fallo y el precio volvio al rango."""
        salida = (
            (dataframe["close"] < dataframe["canal_bajo"])
            & (dataframe["volume"] > 0)
            & dataframe["canal_bajo"].notna()
        )
        dataframe.loc[salida, ["exit_long", "exit_tag"]] = (1, "ruptura_fallida")
        return dataframe
