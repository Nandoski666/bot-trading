"""
Orochi — perfil de volumen y area de valor (adaptacion a cripto spot).

QUE ES OROCHI Y QUE SE PUEDE IMPLEMENTAR DE VERDAD
===================================================
Conviene decirlo antes que nada, porque el nombre promete mas de lo que ningun
bot puede cumplir.

"Orochi" no es una estrategia de Freqtrade. Es un *framework discrecional de
order flow* para futuros del Nasdaq (NQ), distribuido como curso de pago, que
se apoya en siete componentes:

    Auction Market Theory · TPO · Volume Profile · VWAP
    Rhythm · Order Flow · Elliott Wave

De esos siete, desde velas OHLCV de Binance se pueden calcular tres y media:

    IMPLEMENTABLE                          NO IMPLEMENTABLE
    ------------------------------------   -------------------------------------
    Volume Profile (POC, VAH, VAL)         Order Flow — necesita el libro y el
    VWAP                                   lado agresor de cada operacion, que
    Auction Market Theory (aceptacion      no viaja en una vela OHLCV
      y rechazo del area de valor)         Elliott Wave — conteo de ondas
    TPO, de forma aproximada y de baja       discrecional, no especificable
      resolucion (velas en vez de              mecanicamente
      periodos de 30 min)                  Rhythm — concepto interpretativo del
                                             curso, sin definicion publica

Esta estrategia implementa la mitad calculable: **perfil de volumen y area de
valor**, que es una tecnica real y bien documentada (market profile), anterior
al framework y no propiedad de nadie. Lo que NO hace es replicar Orochi: sin
order flow y sin conteo de ondas, no es lo mismo y no hay que pretender que lo
sea.

Tratala como lo que es: una estrategia de perfil de volumen inspirada en la
parte objetiva de ese marco.

LA HIPOTESIS DE MERCADO
=======================
La teoria de subasta dice que el mercado pasa la mayor parte del tiempo
*equilibrado*, rotando alrededor de un precio de consenso, y solo a ratos se
desequilibra en tendencia.

  * **POC** (Point of Control): el precio donde mas volumen se negocio. Es el
    consenso: donde comprador y vendedor mas se pusieron de acuerdo.
  * **Area de valor** (VAL-VAH): la banda que concentra el 70 % del volumen.
    Es donde el mercado considera que el precio "vale".

Cuando el precio sale por debajo del area de valor y **vuelve a entrar**, la
subasta probo precios mas bajos y no encontro vendedores dispuestos a
sostenerlos: el intento de desequilibrio fracaso. La lectura es que el precio
deberia rotar de vuelta hacia el POC.

Eso es lo que compra esta estrategia: **el rechazo de precios por debajo del
valor, confirmado por la reentrada.**

Es una hipotesis distinta de la de BaselineTrend. Aquella compra continuacion
de tendencia; esta compra vuelta a la media dentro de un rango. Que sean
distintas es justamente el motivo de tener las dos.

SESGO DE ANTICIPACION
=====================
El perfil de volumen es el sitio mas facil de este proyecto para colar el
futuro sin darse cuenta: basta calcular el perfil "del periodo" incluyendo
velas posteriores a la que se evalua.

Aqui se evita asi:
  * el perfil de cada vela se calcula sobre una ventana que termina en la vela
    ANTERIOR (`shift(1)` sobre el resultado), nunca sobre la actual;
  * se recalcula cada 24 velas y se propaga hacia adelante con `ffill()`, que
    solo puede copiar valores del pasado. Nunca `bfill()`.
"""

from __future__ import annotations

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

# --- Parametros del perfil ---------------------------------------------------
VENTANA_PERFIL = 168        # velas de 1h = 1 semana de subasta
BINS_PRECIO = 60            # resolucion del histograma de precios
PORCENTAJE_VALOR = 0.70     # 70 % del volumen define el area de valor
RECALCULO_CADA = 24         # se recalcula una vez al dia, no en cada vela
VWAP_PERIODO = 168          # VWAP rodante de la misma ventana
RSI_PERIODO = 14
RSI_MAXIMO = 68             # no comprar en clímax
VOLUMEN_SMA = 20


def _perfil_de_ventana(altos: np.ndarray, bajos: np.ndarray,
                       volumenes: np.ndarray) -> tuple[float, float, float]:
    """POC, VAL y VAH de una ventana de velas.

    El volumen de cada vela se reparte uniformemente entre los bins que cubre su
    rango. Es una aproximacion —dentro de una vela no sabemos donde se negocio
    de verdad— pero es la unica posible sin datos tick a tick, y es la que usa
    todo el mundo que construye perfiles desde OHLCV.

    El area de valor se expande desde el POC hacia el lado con mas volumen,
    alternando, hasta cubrir el 70 % del total. Es el metodo estandar del
    market profile.
    """
    minimo, maximo = float(np.min(bajos)), float(np.max(altos))
    if not np.isfinite(minimo) or not np.isfinite(maximo) or maximo <= minimo:
        return np.nan, np.nan, np.nan

    bordes = np.linspace(minimo, maximo, BINS_PRECIO + 1)
    centros = (bordes[:-1] + bordes[1:]) / 2
    histograma = np.zeros(BINS_PRECIO)

    for alto, bajo, vol in zip(altos, bajos, volumenes):
        if vol <= 0 or not np.isfinite(vol):
            continue
        # Bins que toca el rango de esta vela.
        i = np.searchsorted(bordes, bajo, side="right") - 1
        j = np.searchsorted(bordes, alto, side="left")
        i = max(i, 0)
        j = min(max(j, i + 1), BINS_PRECIO)
        histograma[i:j] += vol / (j - i)

    total = histograma.sum()
    if total <= 0:
        return np.nan, np.nan, np.nan

    idx_poc = int(np.argmax(histograma))
    poc = float(centros[idx_poc])

    # Expansion desde el POC hasta cubrir el 70 % del volumen.
    acumulado = histograma[idx_poc]
    bajo_i = alto_i = idx_poc
    objetivo = total * PORCENTAJE_VALOR
    while acumulado < objetivo and (bajo_i > 0 or alto_i < BINS_PRECIO - 1):
        vol_abajo = histograma[bajo_i - 1] if bajo_i > 0 else -1.0
        vol_arriba = histograma[alto_i + 1] if alto_i < BINS_PRECIO - 1 else -1.0
        if vol_arriba >= vol_abajo:
            alto_i += 1
            acumulado += histograma[alto_i]
        else:
            bajo_i -= 1
            acumulado += histograma[bajo_i]

    return poc, float(centros[bajo_i]), float(centros[alto_i])


class Orochi(EstrategiaBase):
    """Perfil de volumen: compra el rechazo de precios por debajo del valor."""

    hipotesis = (
        "El mercado pasa la mayor parte del tiempo equilibrado, rotando "
        "alrededor de un precio de consenso. Cuando el precio sale por debajo "
        "del area de valor y vuelve a entrar, la subasta probo precios mas "
        "bajos y no los sostuvo: el desequilibrio fracaso y el precio deberia "
        "rotar hacia el POC."
    )

    # La ventana del perfil (168) mas margen para que el VWAP y la EMA(200)
    # converjan. 600 cubre las tres con holgura.
    startup_candle_count: int = 600

    plot_config = {
        "main_plot": {
            "poc": {"color": "#f39c12", "width": 2},
            "valor_alto": {"color": "#27ae60"},
            "valor_bajo": {"color": "#c0392b"},
            "vwap": {"color": "#2980b9"},
        },
        "subplots": {"RSI": {"rsi": {"color": "#8e44ad"}}},
    }

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # --- VWAP rodante ----------------------------------------------------
        # Precio medio ponderado por volumen. Es la referencia que usan las mesas
        # institucionales para juzgar si compraron bien o mal, y por eso actua
        # como iman e imprime soportes y resistencias reales.
        tipico = (dataframe["high"] + dataframe["low"] + dataframe["close"]) / 3
        pv = (tipico * dataframe["volume"]).rolling(VWAP_PERIODO).sum()
        vol = dataframe["volume"].rolling(VWAP_PERIODO).sum()
        dataframe["vwap"] = pv / vol.replace(0, np.nan)

        # --- Perfil de volumen -----------------------------------------------
        # Recalculado cada RECALCULO_CADA velas por coste: reconstruir el
        # histograma en cada una de 50.000 velas y por cada par multiplicaria el
        # tiempo de backtest sin cambiar el resultado de forma apreciable — un
        # perfil semanal no se mueve de una hora a otra.
        altos = dataframe["high"].to_numpy()
        bajos = dataframe["low"].to_numpy()
        vols = dataframe["volume"].to_numpy()
        n = len(dataframe)

        poc = np.full(n, np.nan)
        val = np.full(n, np.nan)
        vah = np.full(n, np.nan)

        for i in range(VENTANA_PERFIL, n, RECALCULO_CADA):
            ini = i - VENTANA_PERFIL
            p, b, a = _perfil_de_ventana(altos[ini:i], bajos[ini:i], vols[ini:i])
            poc[i], val[i], vah[i] = p, b, a

        dataframe["poc"] = poc
        dataframe["valor_bajo"] = val
        dataframe["valor_alto"] = vah

        # ffill propaga el ultimo perfil calculado hacia ADELANTE. Solo copia
        # valores del pasado; nunca bfill, que traeria el futuro.
        for col in ("poc", "valor_bajo", "valor_alto"):
            dataframe[col] = dataframe[col].ffill()
            # Y una vela mas de retraso: el perfil de la vela i se calculo con
            # datos que terminan en i-1, pero este shift lo hace explicito e
            # inmune a cualquier despiste futuro al tocar el bucle de arriba.
            dataframe[col] = dataframe[col].shift(1)

        # --- Posicion del precio respecto al valor ----------------------------
        dataframe["bajo_valor"] = dataframe["close"] < dataframe["valor_bajo"]
        dataframe["dentro_valor"] = (
            (dataframe["close"] >= dataframe["valor_bajo"])
            & (dataframe["close"] <= dataframe["valor_alto"])
        )

        # --- Filtros de apoyo -------------------------------------------------
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=RSI_PERIODO)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=ATR_PERIODO)
        dataframe["volumen_sma"] = ta.SMA(dataframe["volume"], timeperiod=VOLUMEN_SMA)
        dataframe["ema_regimen"] = ta.EMA(dataframe, timeperiod=200)

        return dataframe

    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Reentrada al area de valor tras haberla perdido por abajo."""
        entrada = (
            # 1. AHORA el precio esta dentro del area de valor...
            dataframe["dentro_valor"]
            # 2. ...y en la vela anterior estaba por debajo. Ese es el evento:
            #    la subasta probo precios mas bajos y volvio. Como el cruce de
            #    EMAs de la baseline, es un evento de una vela y no un estado.
            & dataframe["bajo_valor"].shift(1).fillna(False)
            # 3. El precio no esta en caida libre: sigue sobre su EMA(200). Sin
            #    este filtro, la estrategia compraria cada rebote de un mercado
            #    bajista, que es como se pierde dinero con reversion a la media.
            & (dataframe["close"] > dataframe["ema_regimen"])
            # 4. Sin clímax de compra: queda recorrido hasta el POC.
            & (dataframe["rsi"] < RSI_MAXIMO)
            # 5. La reentrada tiene participacion detras.
            & (dataframe["volume"] > dataframe["volumen_sma"])
            & (dataframe["volume"] > 0)
            # Guardas: durante el calentamiento el perfil aun es NaN.
            & dataframe["poc"].notna()
            & dataframe["atr"].notna()
            & dataframe["ema_regimen"].notna()
        )
        dataframe.loc[entrada, ["enter_long", "enter_tag"]] = (1, "reentrada_valor")
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        """Objetivo cumplido (llego al POC) o tesis rota (se fue del valor por arriba).

        Las otras dos salidas —stop inicial y trailing por ATR— las gestiona
        EstrategiaBase, igual que en todas las demas estrategias.
        """
        salida = (
            (
                # Objetivo: el precio rotó hasta el consenso. La tesis se
                # cumplio; lo que venga despues ya es otra operacion.
                (dataframe["close"] >= dataframe["poc"])
                & (dataframe["close"].shift(1) < dataframe["poc"].shift(1))
            )
            | (
                # Tesis agotada: el precio se fue por encima del area de valor.
                # Ya no hay rotacion hacia el POC que capturar.
                dataframe["close"] > dataframe["valor_alto"]
            )
        ) & (dataframe["volume"] > 0) & dataframe["poc"].notna()

        dataframe.loc[salida, ["exit_long", "exit_tag"]] = (1, "rotacion_completada")
        return dataframe
