"""
EstrategiaBase — el riesgo, en un solo sitio, para todas las estrategias.

Por que existe
--------------
El proyecto pasa de una estrategia a cinco. Cada una tiene su propia hipotesis
sobre el mercado y sus propias senales — esa es la parte que debe variar.

Lo que NO puede variar es el riesgo. Si cada estrategia trajera su propio
dimensionamiento y su propio stop, en unas semanas habria cinco versiones
ligeramente distintas de la misma regla, cuatro de ellas sin revisar, y bastaria
que una estuviera mal para vaciar la cuenta. Es el fallo mas comun al pasar de
un bot a varios.

Aqui vive todo eso, una sola vez:

  * dimensionamiento por riesgo   (0.5 % del equity por operacion)
  * stop inicial                  (entrada - 2 x ATR, ATR congelado en la entrada)
  * trailing                      (se arma en +1.5 x ATR, arrastra a 1 x ATR)
  * limite de posiciones          (3 simultaneas)
  * protecciones                  (cooldown, drawdown maximo, racha de stops)
  * lectura sin lookahead         (_vela_cerrada_antes_de)

Una estrategia hija solo implementa `populate_indicators`,
`populate_entry_trend` y `populate_exit_trend`. Si intenta redefinir algo del
riesgo, `tests/test_estrategias.py` falla.

SESGO DE ANTICIPACION
---------------------
Los callbacks de esta clase nunca leen `dataframe.iloc[-1]` a ciegas: usan
`_vela_cerrada_antes_de()`, que filtra por `date < momento`. En backtest el
dataprovider expone la vela que se esta procesando, cuyo high/low/close aun no
habian ocurrido en el instante de decidir. Ver la explicacion completa en
BaselineTrend.py.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_DIR = str(Path(__file__).resolve().parent)
if _DIR not in sys.path:
    sys.path.insert(0, _DIR)

import pandas as pd
from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy
from pandas import DataFrame

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


class EstrategiaBase(IStrategy):
    """Riesgo comun. Las hijas solo aportan indicadores y senales."""

    INTERFACE_VERSION = 3

    _aviso_max_trades_emitido = False

    # --- Hipotesis de mercado -----------------------------------------------
    # Cada estrategia hija DEBE declarar que cree que hace el mercado y por que
    # sus senales lo capturan. No es decoracion: el plan prohibe anadir
    # indicadores sin justificar que hipotesis capturan, y una frase obliga a
    # tenerla clara antes de escribir el codigo.
    hipotesis: str = "SIN DECLARAR"

    # --- Marco temporal ------------------------------------------------------
    timeframe = "1h"
    startup_candle_count: int = 600
    process_only_new_candles = True

    # --- Direccion -----------------------------------------------------------
    can_short = False           # spot: no se vende lo que no se tiene

    # --- Salidas -------------------------------------------------------------
    # Sin objetivo de beneficio fijo: cortaria las ganadoras largas, que son las
    # que pagan a las perdedoras en un sistema seguidor de tendencia.
    minimal_roi = {"0": 10.0}

    stoploss = STOPLOSS_BACKSTOP
    use_custom_stoploss = True
    trailing_stop = False       # el trailing va en ATR, no en porcentaje fijo

    use_exit_signal = True
    exit_profit_only = False
    ignore_roi_if_entry_signal = False

    # --- Ordenes -------------------------------------------------------------
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
    ignore_buying_expired_candle_after = 7200   # 2 velas de 1h

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
            if not EstrategiaBase._aviso_max_trades_emitido:
                logger.warning(
                    "max_open_trades del config (%s) excede el limite duro de %s. "
                    "Se aplica el limite duro.", configurado, MAX_POSICIONES_SIMULTANEAS
                )
                EstrategiaBase._aviso_max_trades_emitido = True
            self.config["max_open_trades"] = MAX_POSICIONES_SIMULTANEAS

        logger.info(
            "%s lista | riesgo/op %.2f%% | max %d posiciones | "
            "stop %sxATR | trailing %sxATR desde +%sxATR",
            type(self).__name__, RIESGO_POR_OPERACION * 100, MAX_POSICIONES_SIMULTANEAS,
            ATR_MULTIPLICADOR_STOP, ATR_DISTANCIA_TRAILING, ATR_ACTIVACION_TRAILING,
        )
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
    # Filtro de contexto con IA (opcional)
    # ==================================================================
    def _ia_permite_operar(self) -> tuple[bool, str]:
        """Consulta el veredicto del filtro de contexto. Nunca lanza.

        Reglas de integracion, todas deliberadas:

        1. **Solo en dry-run y live.** En backtest e hyperopt se ignora por
           completo. Si el filtro afectara al backtest, ningun resultado
           historico volveria a ser reproducible — y sin backtest reproducible
           no queda forma de saber si el sistema funciona.

        2. **Solo puede vetar.** Lee un archivo que otro proceso escribe; jamas
           genera una entrada. La estrategia decide QUE comprar; el filtro solo
           puede decir "hoy no".

        3. **Falla abierto.** Sin archivo, con el archivo caducado, corrupto o
           con la API caida, se opera. Que se caiga un servicio externo no puede
           dejar las posiciones sin gestionar.

        4. **Caduca.** Una opinion de hace ocho horas sobre el mercado de ahora
           no es informacion. Pasada su vigencia se descarta sola.
        """
        modo = str(self.config.get("runmode", "")).lower()
        if "backtest" in modo or "hyperopt" in modo or "edge" in modo:
            return True, ""

        try:
            import json
            from datetime import datetime, timedelta, timezone

            archivo = Path(self.config.get("user_data_dir",
                                           Path(__file__).resolve().parents[1])) / "decision_ia.json"
            if not archivo.exists():
                return True, ""

            d = json.loads(archivo.read_text(encoding="utf-8"))
            momento = datetime.fromisoformat(d["momento"])
            if momento.tzinfo is None:
                momento = momento.replace(tzinfo=timezone.utc)
            vigencia = timedelta(hours=float(d.get("vigencia_horas", 6)))
            if datetime.now(timezone.utc) - momento > vigencia:
                return True, ""       # caducado: se ignora

            if d.get("operar", True):
                return True, ""
            return False, str(d.get("motivo", "sin motivo"))[:200]
        except Exception:             # noqa: BLE001
            # Cualquier problema leyendo el veredicto: se opera. Es la unica
            # opcion segura — un filtro roto no puede paralizar el sistema.
            return True, ""

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

        permite, motivo = self._ia_permite_operar()
        if not permite:
            logger.info("Entrada en %s vetada por el filtro de contexto: %s",
                        pair, motivo)
            return False

        abiertas = Trade.get_open_trade_count()
        if abiertas >= MAX_POSICIONES_SIMULTANEAS:
            logger.warning(
                "Entrada en %s rechazada: ya hay %d posiciones abiertas (maximo %d).",
                pair, abiertas, MAX_POSICIONES_SIMULTANEAS,
            )
            return False

        return True
