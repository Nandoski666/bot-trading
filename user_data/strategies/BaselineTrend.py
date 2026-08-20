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

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Freqtrade carga las estrategias por ruta, no como paquete. En el proceso
# principal eso basta, pero el hyperopt reparte el trabajo entre procesos hijo
# que vuelven a importar este archivo desde cero — y alli `from reglas_riesgo
# import ...` fallaria porque el directorio no esta en sys.path.
_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import pandas as pd
import talib.abstract as ta
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame
from technical import qtpylib

from reglas_riesgo import (
    ATR_ACTIVACION_TRAILING,
    ATR_DISTANCIA_TRAILING,
    ATR_MULTIPLICADOR_STOP,
    ATR_PERIODO,
    MAX_POSICIONES_SIMULTANEAS,
    RIESGO_POR_OPERACION,
    STOPLOSS_BACKSTOP,
    validar_reglas,
)

logger = logging.getLogger(__name__)

# --- Parametros de la senal (seccion 2 del plan) ---------------------------
# A diferencia de las reglas de riesgo, estos SI son candidatos legitimos a
# hyperopt en T6. Se dejan como constantes con nombre para que el walk-forward
# sepa exactamente que puede tocar y que no.
EMA_RAPIDA = 20
EMA_LENTA = 50
EMA_REGIMEN = 200
RSI_PERIODO = 14
RSI_MINIMO = 40
RSI_MAXIMO = 70
VOLUMEN_SMA = 20


class BaselineTrend(IStrategy):
    INTERFACE_VERSION = 3

    # Evita repetir el mismo aviso una vez por instancia de estrategia cuando
    # herramientas como lookahead-analysis crean docenas de ellas.
    _aviso_max_trades_emitido = False

    # --- Marco temporal ----------------------------------------------------
    timeframe = "1h"

    # Velas de calentamiento que Freqtrade descarta antes de permitir senales.
    #
    # El plan fijaba 200 (= periodo de la EMA mas larga). La medicion dice que
    # no basta: `freqtrade recursive-analysis` sobre BTC/USDT muestra que con
    # 200 velas la EMA(200) todavia se desvia **-0.63 %** de su valor
    # convergido, mientras que con 400+ el error cae a -0.014 %.
    #
    # El motivo es que una EMA es un filtro de respuesta infinita: nunca olvida
    # del todo su valor inicial, solo lo amortigua. Arrancarla con exactamente
    # su periodo deja el 37 % del peso todavia contaminado por la semilla.
    #
    # Importa porque `close > ema_regimen` es el filtro que mas operaciones
    # descarta. Un sesgo de 0.6 % en esa linea cambia que operaciones existen
    # en el backtest, y no de la misma forma en que cambiarian en vivo (donde
    # el bot siempre tiene anos de historico detras). Es decir: con 200 el
    # backtest y la produccion no miden lo mismo.
    #
    # 600 = 3 x el periodo de la EMA mas larga. Coste: ~25 dias de datos
    # descartados al inicio de cada ventana. Es un precio barato.
    startup_candle_count: int = 600

    # Solo procesar cuando cierra una vela nueva. Reevaluar tick a tick no
    # aporta nada en 1h y multiplica las llamadas al exchange.
    process_only_new_candles = True

    # --- Direccion ---------------------------------------------------------
    can_short = False  # spot: no se puede vender lo que no se tiene

    # --- Salidas -----------------------------------------------------------
    # Sin objetivos de beneficio fijos: la estrategia sale por cruce de EMAs,
    # por stop o por trailing. Un ROI fijo cortaria justamente las operaciones
    # ganadoras largas, que son las que pagan a las perdedoras en un sistema
    # seguidor de tendencia.
    minimal_roi = {"0": 10.0}  # 1000 %: en la practica, desactivado

    # Backstop de Freqtrade. El stop real lo calcula custom_stoploss.
    stoploss = STOPLOSS_BACKSTOP
    use_custom_stoploss = True

    # El trailing lo gestionamos nosotros en unidades de ATR, no en porcentaje
    # fijo. El trailing nativo de Freqtrade queda desactivado para que no haya
    # dos mecanismos compitiendo.
    trailing_stop = False

    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    # --- Ordenes -----------------------------------------------------------
    # A mercado: en un sistema de 1h, perseguir 5 puntos basicos con ordenes
    # limit que no llenan deja posiciones sin gestionar, que es mucho peor.
    order_types = {
        "entry": "market",
        "exit": "market",
        "emergency_exit": "market",
        "force_entry": "market",
        "force_exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }
    order_time_in_force = {"entry": "GTC", "exit": "GTC"}

    # Si la senal no se pudo ejecutar en 2 velas, ya no es la misma situacion
    # de mercado. Se descarta en vez de entrar tarde.
    ignore_buying_expired_candle_after = 7200  # segundos = 2 velas de 1h

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

    # ------------------------------------------------------------------
    # Protecciones (seccion 3 del plan). Se refuerzan en T9 con chequeos
    # propios; estas son las que Freqtrade aplica por si mismo.
    # ------------------------------------------------------------------
    @property
    def protections(self):
        return [
            {
                # Tras cerrar una posicion, no volver a entrar en ese par en la
                # vela siguiente. Evita reentrar en el mismo latigazo.
                "method": "CooldownPeriod",
                "stop_duration_candles": 2,
            },
            {
                # Si el drawdown supera el limite total en la ventana reciente,
                # se bloquea TODO el bot. Es la version de Freqtrade del kill
                # switch; T9 anade la verificacion externa e independiente.
                "method": "MaxDrawdown",
                "lookback_period_candles": 168,   # 7 dias
                "trade_limit": 5,
                "stop_duration_candles": 168,
                "max_allowed_drawdown": 0.10,
            },
            {
                # Racha de stops en poco tiempo: senal de que el regimen cambio.
                # Parar 12 h y dejar que el humano mire.
                "method": "StoplossGuard",
                "lookback_period_candles": 24,
                "trade_limit": 3,
                "stop_duration_candles": 12,
                "only_per_pair": False,
            },
        ]

    # ==================================================================
    # Arranque
    # ==================================================================
    def bot_start(self, **kwargs) -> None:
        """Se ejecuta una vez al arrancar (live, dry-run y backtest)."""
        validar_reglas()

        # El limite de posiciones vive en dos sitios (config y reglas_riesgo).
        # Si divergen, gana el mas restrictivo. Algunas herramientas de
        # Freqtrade (lookahead-analysis, hyperopt) ponen -1 = ilimitado; aqui se
        # corrige siempre, pero se avisa una sola vez para no inundar el log.
        configurado = self.config.get("max_open_trades", MAX_POSICIONES_SIMULTANEAS)
        if configurado in (-1, float("inf")) or configurado > MAX_POSICIONES_SIMULTANEAS:
            if not BaselineTrend._aviso_max_trades_emitido:
                logger.warning(
                    "max_open_trades del config (%s) excede el limite duro de %s. "
                    "Se aplica el limite duro.", configurado, MAX_POSICIONES_SIMULTANEAS
                )
                BaselineTrend._aviso_max_trades_emitido = True
            self.config["max_open_trades"] = MAX_POSICIONES_SIMULTANEAS

        logger.info(
            "BaselineTrend lista | riesgo/op %.2f%% | max %d posiciones | "
            "stop %sxATR | trailing %sxATR desde +%sxATR",
            RIESGO_POR_OPERACION * 100, MAX_POSICIONES_SIMULTANEAS,
            ATR_MULTIPLICADOR_STOP, ATR_DISTANCIA_TRAILING, ATR_ACTIVACION_TRAILING,
        )

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

    # ==================================================================
    # Utilidad: acceso a datos SIN mirar al futuro
    # ==================================================================
    def _vela_cerrada_antes_de(self, pair: str, momento: datetime) -> pd.Series | None:
        """Devuelve la ultima vela **cerrada** estrictamente anterior a `momento`.

        Este metodo es el corazon de la proteccion contra lookahead en los
        callbacks. Escribir `dataframe.iloc[-1]` seria mas corto y estaria mal:

          * En backtest, el dataprovider expone el dataframe hasta la vela que
            se esta procesando, incluida. Esa vela es justamente la que aun no
            ha terminado de ocurrir desde el punto de vista de la decision.
          * Filtrar por `date < momento` deja fuera esa vela y devuelve la
            anterior, que es la unica que estaba realmente disponible.

        En vivo el resultado es el mismo, porque Freqtrade ya descarta la vela
        parcial de Binance. Es decir: el mismo codigo se comporta igual en
        backtest y en produccion, que es exactamente lo que se busca.
        """
        dataframe, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if dataframe is None or dataframe.empty:
            return None

        if momento.tzinfo is None:
            momento = momento.replace(tzinfo=timezone.utc)

        cerradas = dataframe.loc[dataframe["date"] < momento]
        if cerradas.empty:
            return None
        return cerradas.iloc[-1]

    # ==================================================================
    # Tamano de posicion por riesgo
    # ==================================================================
    def custom_stake_amount(
        self,
        pair: str,
        current_time: datetime,
        current_rate: float,
        proposed_stake: float,
        min_stake: float | None,
        max_stake: float,
        leverage: float,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> float:
        """Dimensiona la posicion para arriesgar exactamente 0.5 % del equity.

        La formula del plan da una CANTIDAD de moneda base:

            cantidad = (equity x 0.005) / (entrada - precio_stop)

        Freqtrade espera un importe en moneda de cotizacion (USDT), asi que:

            stake = cantidad x entrada
                  = equity x 0.005 x entrada / (entrada - precio_stop)
                  = equity x 0.005 / distancia_relativa_al_stop

        Lo importante: el tamano NO es fijo. Cuando la volatilidad sube, el
        stop se aleja y la posicion se hace mas pequena. La perdida en dolares
        si salta el stop es la misma en los dos casos. Eso es lo que hace que
        el riesgo sea comparable entre pares y entre epocas.
        """
        vela = self._vela_cerrada_antes_de(pair, current_time)

        # Sin datos o sin ATR valido no hay forma de dimensionar por riesgo.
        # Antes que adivinar, se rechaza la entrada devolviendo 0.
        if vela is None or pd.isna(vela.get("atr")) or vela["atr"] <= 0:
            logger.warning("%s: sin ATR utilizable en %s, se omite la entrada.",
                           pair, current_time)
            return 0.0

        atr = float(vela["atr"])
        distancia_stop = ATR_MULTIPLICADOR_STOP * atr

        if current_rate <= 0 or distancia_stop <= 0:
            return 0.0

        # Distancia al stop como fraccion del precio de entrada.
        distancia_relativa = distancia_stop / current_rate

        # Equity total = lo disponible + lo ya inmovilizado en posiciones
        # abiertas. Usar solo el saldo libre encogeria el tamano de cada nueva
        # posicion a medida que se abren otras, que no es lo que dice la regla.
        equity = self.wallets.get_total_stake_amount() if self.wallets else max_stake
        riesgo_moneda = equity * RIESGO_POR_OPERACION
        stake = riesgo_moneda / distancia_relativa

        # --- Topes ----------------------------------------------------------
        # Con ATR muy bajo la formula pide una posicion enorme (stop a 0.3 % =>
        # 167 % del equity). Se limita a la parte proporcional del capital para
        # que quepan las 3 posiciones. El tope solo puede REDUCIR el riesgo,
        # nunca aumentarlo, asi que es seguro por construccion.
        tope_por_posicion = equity / MAX_POSICIONES_SIMULTANEAS
        if stake > tope_por_posicion:
            logger.info(
                "%s: stake %.2f recortado a %.2f (tope 1/%d del equity). "
                "El riesgo efectivo baja del %.2f%%.",
                pair, stake, tope_por_posicion, MAX_POSICIONES_SIMULTANEAS,
                RIESGO_POR_OPERACION * 100,
            )
            stake = tope_por_posicion

        # Respetar lo que el exchange y la cartera permiten.
        stake = min(stake, max_stake)

        # Si ni siquiera llega al minimo del exchange, no se entra. Forzar el
        # minimo significaria arriesgar mas del 0.5 % pactado.
        if min_stake is not None and stake < min_stake:
            logger.info("%s: stake %.2f por debajo del minimo %.2f, se omite.",
                        pair, stake, min_stake)
            return 0.0

        return stake

    # ==================================================================
    # Stop dinamico: inicial por ATR + trailing por ATR
    # ==================================================================
    def custom_stoploss(
        self,
        pair: str,
        trade: Trade,
        current_time: datetime,
        current_rate: float,
        current_profit: float,
        after_fill: bool,
        **kwargs,
    ) -> float | None:
        """Stop en unidades de ATR, congelado el ATR de la vela de entrada.

        Dos fases:

          Fase 1 — proteccion
            stop = entrada - 2 x ATR.  Fijo. Define la perdida maxima de la
            operacion y es la distancia con la que se calculo el tamano.

          Fase 2 — trailing
            Cuando el maximo alcanzado supera entrada + 1.5 x ATR, el stop pasa
            a arrastrarse a 1 x ATR por debajo del maximo. El hueco entre 1.5 y
            1.0 asegura que al activarse el trailing el stop ya esta en
            beneficio (+0.5 x ATR) y no cierra la posicion en el acto.

        Freqtrade solo mueve el stop en direccion favorable, asi que un stop
        que ya subio nunca vuelve a bajar aunque este metodo devuelva algo peor.
        """
        atr = self._atr_de_entrada(pair, trade)
        if atr is None or atr <= 0:
            # Sin ATR fiable se deja el backstop de la clase. No es lo ideal,
            # pero es mejor que devolver un stop inventado.
            return None

        # --- Fase 1: stop inicial ------------------------------------------
        stop_precio = trade.open_rate - ATR_MULTIPLICADOR_STOP * atr

        # --- Fase 2: trailing ----------------------------------------------
        # trade.max_rate es el maximo que alcanzo el precio desde la apertura
        # (marca de agua). Usarlo, y no current_rate, es lo que hace que el
        # stop no retroceda cuando el precio corrige.
        maximo = trade.max_rate or trade.open_rate
        recorrido = maximo - trade.open_rate

        if recorrido >= ATR_ACTIVACION_TRAILING * atr:
            stop_trailing = maximo - ATR_DISTANCIA_TRAILING * atr
            stop_precio = max(stop_precio, stop_trailing)

        # Freqtrade espera el stop como ratio relativo al precio actual:
        #   stop_absoluto = current_rate * (1 + valor_devuelto)
        if current_rate <= 0:
            return None
        return stop_precio / current_rate - 1

    def _atr_de_entrada(self, pair: str, trade: Trade) -> float | None:
        """ATR de la vela que genero la senal. Se calcula una vez y se guarda.

        Congelarlo es deliberado: si el stop se recalculara con el ATR actual,
        una subida de volatilidad posterior alejaria el stop de una posicion ya
        abierta —aumentando la perdida maxima por encima del 0.5 % pactado— y
        una caida de volatilidad lo acercaria, cerrando por ruido operaciones
        que se dimensionaron con otro criterio.
        """
        guardado = trade.get_custom_data("atr_entrada")
        if guardado is not None:
            return float(guardado)

        # La vela de la senal es la ultima CERRADA antes de la apertura del
        # trade. `_vela_cerrada_antes_de` garantiza que no se lee la vela en la
        # que se ejecuto la entrada (cuyo cierre aun no se conocia al decidir).
        vela = self._vela_cerrada_antes_de(pair, trade.open_date_utc)
        if vela is None or pd.isna(vela.get("atr")) or vela["atr"] <= 0:
            return None

        atr = float(vela["atr"])
        trade.set_custom_data("atr_entrada", atr)
        return atr

    # ==================================================================
    # Confirmacion final antes de mandar la orden
    # ==================================================================
    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs,
    ) -> bool:
        """Ultima verja antes de mandar la orden al exchange.

        Freqtrade ya respeta `max_open_trades`, pero esta comprobacion es
        independiente y barata: si un cambio de configuracion, un reinicio a
        medias o un bug dejara pasar una cuarta posicion, aqui se corta. Las
        reglas de riesgo se defienden en mas de una capa a proposito.
        """
        if side != "long":
            logger.error("Intento de abrir %s en spot. Rechazado.", side)
            return False

        abiertas = Trade.get_open_trade_count()
        if abiertas >= MAX_POSICIONES_SIMULTANEAS:
            logger.warning(
                "Entrada en %s rechazada: ya hay %d posiciones abiertas (maximo %d).",
                pair, abiertas, MAX_POSICIONES_SIMULTANEAS,
            )
            return False

        return True
