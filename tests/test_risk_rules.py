"""
T4 — Tests de las reglas de riesgo (seccion 3 del plan).

Estos son los tests que importan. Un error en la logica de senales cuesta
operaciones mediocres; un error en el dimensionamiento o en el stop cuesta la
cuenta entera.

Lo que se verifica:
  * el tamano de posicion nunca arriesga mas del 0.5 % del equity
  * el stop se calcula bien con distintos valores de ATR
  * el trailing se activa donde debe y no antes
  * no se abren mas de 3 posiciones
  * las constantes de riesgo no son optimizables por hyperopt
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import TradeFalso, construir_ohlcv_atr, estrategia_con_datos

import reglas_riesgo as R


AHORA = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


# ===========================================================================
# Las constantes son las del plan y no son optimizables
# ===========================================================================

def test_constantes_coinciden_con_el_plan():
    """Si alguien cambia una cifra de riesgo, este test lo detiene."""
    assert R.RIESGO_POR_OPERACION == 0.005,        "riesgo por operacion = 0.5 %"
    assert R.MAX_POSICIONES_SIMULTANEAS == 3,      "maximo 3 posiciones"
    assert R.PERDIDA_DIARIA_MAXIMA == 0.03,        "perdida diaria maxima = 3 %"
    assert R.DRAWDOWN_TOTAL_MAXIMO == 0.10,        "drawdown total maximo = 10 %"
    assert R.APALANCAMIENTO == 1.0,                "solo spot, sin apalancamiento"
    assert R.PERMITE_CORTOS is False
    assert R.PERMITE_PROMEDIAR_BAJA is False
    assert R.ATR_MULTIPLICADOR_STOP == 2.0
    assert R.ATR_ACTIVACION_TRAILING == 1.5
    assert R.ATR_DISTANCIA_TRAILING == 1.0


def test_validacion_interna_pasa():
    """Las invariantes entre reglas son coherentes entre si."""
    R.validar_reglas()


def test_reglas_no_son_hiperoptimizables():
    """Ninguna regla de riesgo puede ser un parametro de Freqtrade.

    El plan lo prohibe explicitamente: si una de estas constantes fuera un
    `DecimalParameter`, el hyperopt encontraria que arriesgar el 5 % por
    operacion mejora el retorno del backtest — y quebraria la cuenta en vivo.
    """
    from freqtrade.strategy.parameters import BaseParameter

    nombres = [n for n in dir(R) if n.isupper()]
    assert nombres, "no se encontro ninguna constante de riesgo"
    for nombre in nombres:
        valor = getattr(R, nombre)
        assert not isinstance(valor, BaseParameter), (
            f"{nombre} es un parametro de hyperopt. Las reglas de riesgo son "
            "constantes por diseno (seccion 3 del plan)."
        )
        assert isinstance(valor, (int, float, bool)), (
            f"{nombre} deberia ser un escalar simple, es {type(valor)}"
        )


def test_estrategia_no_expone_parametros_optimizables(estrategia):
    """La baseline no tiene espacio de hyperopt: es la referencia, no el
    resultado de una busqueda. Optimizar la propia linea base la invalidaria
    como punto de comparacion.

    `ft_load_hyper_params()` es imprescindible: Freqtrade rellena el registro de
    parametros en `ft_bot_start()`, no en el constructor. Sin esa llamada,
    `enumerate_parameters()` devuelve siempre una lista vacia y este test
    pasaria aunque la estrategia tuviera veinte parametros abiertos. Un test que
    no puede fallar es peor que no tener test: da confianza sin comprobar nada.
    """
    estrategia.ft_load_hyper_params(False)
    optimizables = [nombre for nombre, _ in estrategia.enumerate_parameters()]
    assert optimizables == [], (
        f"la baseline expone parametros optimizables: {optimizables}. "
        "La linea base es el punto de comparacion; optimizarla la invalida."
    )


# ===========================================================================
# Tamano de posicion: nunca mas del 0.5 %
# ===========================================================================

@pytest.mark.parametrize("atr,precio", [
    (10.0,  1000.0),   # stop 2 % -> posicion grande
    (25.0,  1000.0),   # stop 5 %
    (50.0,  1000.0),   # stop 10 %
    (100.0, 1000.0),   # stop 20 % -> posicion pequena
    (2.0,     50.0),   # otro par, otro orden de magnitud
    (500.0, 60000.0),  # BTC
])
def test_riesgo_por_operacion_nunca_supera_el_limite(estrategia, atr, precio):
    """Sea cual sea el ATR, la perdida al tocar el stop es <= 0.5 % del equity.

    Se recorre el calculo al reves: se toma el stake que devuelve la estrategia,
    se deduce la cantidad comprada, y se calcula cuanto se pierde si el precio
    llega al stop. Esa cifra es la que no puede pasar del limite.
    """
    equity = 10_000.0
    df = construir_ohlcv_atr(300, precio=precio, rango=atr)
    s = estrategia_con_datos(estrategia, estrategia.populate_indicators(df, {"pair": "BTC/USDT"}),
                             equity=equity)

    stake = s.custom_stake_amount(
        pair="BTC/USDT", current_time=AHORA, current_rate=precio,
        proposed_stake=1000.0, min_stake=10.0, max_stake=equity,
        leverage=1.0, entry_tag=None, side="long",
    )
    assert stake > 0, "no se dimensiono ninguna posicion"

    atr_real = s.dp._df["atr"].iloc[-1]
    cantidad = stake / precio
    precio_stop = precio - R.ATR_MULTIPLICADOR_STOP * atr_real
    perdida = cantidad * (precio - precio_stop)

    riesgo_efectivo = perdida / equity
    assert riesgo_efectivo <= R.RIESGO_POR_OPERACION + 1e-9, (
        f"con ATR={atr_real:.4f} el riesgo efectivo seria {riesgo_efectivo:.4%}, "
        f"por encima del limite de {R.RIESGO_POR_OPERACION:.2%}"
    )


def test_riesgo_es_exactamente_el_pactado_cuando_no_topa(estrategia):
    """Sin toparse con el limite de tamano, el riesgo es exactamente 0.5 %.

    El test anterior comprueba la cota superior; este comprueba que no nos
    quedamos sistematicamente cortos, que seria igual de incorrecto.
    """
    equity, precio, atr = 10_000.0, 1000.0, 40.0   # stop 8 % -> stake 6.25 % del equity
    df = construir_ohlcv_atr(300, precio=precio, rango=atr)
    s = estrategia_con_datos(estrategia, estrategia.populate_indicators(df, {"pair": "BTC/USDT"}),
                             equity=equity)

    stake = s.custom_stake_amount(
        pair="BTC/USDT", current_time=AHORA, current_rate=precio,
        proposed_stake=1000.0, min_stake=10.0, max_stake=equity,
        leverage=1.0, entry_tag=None, side="long",
    )
    atr_real = s.dp._df["atr"].iloc[-1]
    perdida = (stake / precio) * (R.ATR_MULTIPLICADOR_STOP * atr_real)

    assert perdida / equity == pytest.approx(R.RIESGO_POR_OPERACION, rel=0.01)


def test_posicion_encoge_cuando_sube_la_volatilidad(estrategia):
    """A mayor ATR, menor tamano. Es la propiedad que hace comparable el riesgo
    entre pares tranquilos y agitados."""
    equity, precio = 10_000.0, 1000.0
    tamanos = []
    for atr in (10.0, 20.0, 40.0, 80.0):
        df = construir_ohlcv_atr(300, precio=precio, rango=atr)
        s = estrategia_con_datos(estrategia,
                                 estrategia.populate_indicators(df, {"pair": "BTC/USDT"}),
                                 equity=equity)
        tamanos.append(s.custom_stake_amount(
            pair="BTC/USDT", current_time=AHORA, current_rate=precio,
            proposed_stake=1000.0, min_stake=10.0, max_stake=equity,
            leverage=1.0, entry_tag=None, side="long",
        ))

    assert tamanos == sorted(tamanos, reverse=True), (
        f"el tamano no decrece con la volatilidad: {tamanos}"
    )


def test_tope_por_posicion_permite_tres_posiciones(estrategia):
    """Con volatilidad muy baja el tope entra en juego y deja sitio a 3 posiciones.

    Sin el tope, un ATR pequeno pediria mas del 100 % del equity en una sola
    posicion. El recorte solo puede bajar el riesgo, nunca subirlo.
    """
    equity, precio, atr = 10_000.0, 1000.0, 1.0   # stop 0.2 % -> pediria 250 % del equity
    df = construir_ohlcv_atr(300, precio=precio, rango=atr)
    s = estrategia_con_datos(estrategia, estrategia.populate_indicators(df, {"pair": "BTC/USDT"}),
                             equity=equity)

    stake = s.custom_stake_amount(
        pair="BTC/USDT", current_time=AHORA, current_rate=precio,
        proposed_stake=1000.0, min_stake=10.0, max_stake=equity,
        leverage=1.0, entry_tag=None, side="long",
    )
    assert stake <= equity / R.MAX_POSICIONES_SIMULTANEAS + 1e-9
    assert stake * R.MAX_POSICIONES_SIMULTANEAS <= equity + 1e-9, (
        "tres posiciones de este tamano no caben en el equity"
    )


def test_sin_atr_no_se_entra(estrategia):
    """Sin ATR valido no hay forma de dimensionar por riesgo: se rechaza."""
    df = construir_ohlcv_atr(300, precio=1000.0, rango=20.0)
    df_ind = estrategia.populate_indicators(df, {"pair": "BTC/USDT"})
    df_ind["atr"] = float("nan")
    s = estrategia_con_datos(estrategia, df_ind, equity=10_000.0)

    stake = s.custom_stake_amount(
        pair="BTC/USDT", current_time=AHORA, current_rate=1000.0,
        proposed_stake=1000.0, min_stake=10.0, max_stake=10_000.0,
        leverage=1.0, entry_tag=None, side="long",
    )
    assert stake == 0.0, "se dimensiono una posicion sin ATR"


def test_stake_bajo_el_minimo_del_exchange_se_rechaza(estrategia):
    """Antes que forzar el minimo del exchange —y con el, arriesgar de mas—
    se prefiere no entrar."""
    df = construir_ohlcv_atr(300, precio=1000.0, rango=200.0)  # stop 40 %
    s = estrategia_con_datos(estrategia, estrategia.populate_indicators(df, {"pair": "BTC/USDT"}),
                             equity=100.0)   # equity minusculo

    stake = s.custom_stake_amount(
        pair="BTC/USDT", current_time=AHORA, current_rate=1000.0,
        proposed_stake=1000.0, min_stake=50.0, max_stake=100.0,
        leverage=1.0, entry_tag=None, side="long",
    )
    assert stake == 0.0


# ===========================================================================
# Stop inicial y trailing
# ===========================================================================

@pytest.mark.parametrize("atr", [5.0, 20.0, 50.0, 137.5])
def test_stop_inicial_esta_a_dos_atr(estrategia, atr):
    """El stop inicial es exactamente entrada - 2 x ATR, para cualquier ATR."""
    precio = 1000.0
    df = estrategia.populate_indicators(construir_ohlcv_atr(300, precio, atr),
                                        {"pair": "BTC/USDT"})
    s = estrategia_con_datos(estrategia, df)
    atr_real = df["atr"].iloc[-1]

    trade = TradeFalso(open_rate=precio, open_date_utc=df["date"].iloc[-1] + timedelta(hours=1))
    ratio = s.custom_stoploss(pair="BTC/USDT", trade=trade, current_time=AHORA,
                              current_rate=precio, current_profit=0.0, after_fill=False)

    stop_absoluto = precio * (1 + ratio)
    esperado = precio - R.ATR_MULTIPLICADOR_STOP * atr_real
    assert stop_absoluto == pytest.approx(esperado, rel=1e-6)
    assert stop_absoluto < precio, "el stop tiene que estar por debajo de la entrada"


def test_trailing_no_se_activa_antes_de_tiempo(estrategia):
    """Con el maximo por debajo de +1.5 x ATR el stop sigue siendo el inicial."""
    precio, atr = 1000.0, 20.0
    df = estrategia.populate_indicators(construir_ohlcv_atr(300, precio, atr),
                                        {"pair": "BTC/USDT"})
    s = estrategia_con_datos(estrategia, df)
    atr_real = df["atr"].iloc[-1]

    # Maximo alcanzado: +1.4 x ATR. Justo por debajo del umbral.
    maximo = precio + 1.4 * atr_real
    trade = TradeFalso(open_rate=precio, max_rate=maximo,
                       open_date_utc=df["date"].iloc[-1] + timedelta(hours=1))

    ratio = s.custom_stoploss(pair="BTC/USDT", trade=trade, current_time=AHORA,
                              current_rate=maximo, current_profit=0.028, after_fill=False)
    stop = maximo * (1 + ratio)
    assert stop == pytest.approx(precio - R.ATR_MULTIPLICADOR_STOP * atr_real, rel=1e-6)


def test_trailing_se_activa_en_el_umbral(estrategia):
    """Superado +1.5 x ATR, el stop pasa a maximo - 1 x ATR y queda en beneficio."""
    precio, atr = 1000.0, 20.0
    df = estrategia.populate_indicators(construir_ohlcv_atr(300, precio, atr),
                                        {"pair": "BTC/USDT"})
    s = estrategia_con_datos(estrategia, df)
    atr_real = df["atr"].iloc[-1]

    maximo = precio + R.ATR_ACTIVACION_TRAILING * atr_real
    trade = TradeFalso(open_rate=precio, max_rate=maximo,
                       open_date_utc=df["date"].iloc[-1] + timedelta(hours=1))

    ratio = s.custom_stoploss(pair="BTC/USDT", trade=trade, current_time=AHORA,
                              current_rate=maximo, current_profit=0.03, after_fill=False)
    stop = maximo * (1 + ratio)

    assert stop == pytest.approx(maximo - R.ATR_DISTANCIA_TRAILING * atr_real, rel=1e-6)
    # El punto del hueco 1.5 -> 1.0: al armarse, el trailing ya asegura beneficio.
    assert stop > precio, (
        "al activarse el trailing el stop deberia estar por encima de la entrada; "
        "si no, se cerraria la posicion en el acto"
    )


def test_trailing_sigue_al_maximo_no_al_precio_actual(estrategia):
    """Si el precio corrige desde el maximo, el stop NO baja con el.

    Usar `current_rate` en vez de `max_rate` seria el bug clasico: el stop
    perseguiria al precio hacia abajo y dejaria de proteger nada.
    """
    precio, atr = 1000.0, 20.0
    df = estrategia.populate_indicators(construir_ohlcv_atr(300, precio, atr),
                                        {"pair": "BTC/USDT"})
    s = estrategia_con_datos(estrategia, df)
    atr_real = df["atr"].iloc[-1]

    maximo = precio + 3.0 * atr_real     # subio bastante
    actual = precio + 2.0 * atr_real     # y luego corrigio
    trade = TradeFalso(open_rate=precio, max_rate=maximo,
                       open_date_utc=df["date"].iloc[-1] + timedelta(hours=1))

    ratio = s.custom_stoploss(pair="BTC/USDT", trade=trade, current_time=AHORA,
                              current_rate=actual, current_profit=0.04, after_fill=False)
    stop = actual * (1 + ratio)

    assert stop == pytest.approx(maximo - R.ATR_DISTANCIA_TRAILING * atr_real, rel=1e-6)
    assert stop > precio


def test_stop_nunca_queda_por_encima_del_precio_actual(estrategia):
    """Un stop por encima del precio cerraria la posicion al instante."""
    precio, atr = 1000.0, 20.0
    df = estrategia.populate_indicators(construir_ohlcv_atr(300, precio, atr),
                                        {"pair": "BTC/USDT"})
    s = estrategia_con_datos(estrategia, df)
    atr_real = df["atr"].iloc[-1]

    for mult_max in (0.0, 1.0, 1.5, 2.0, 5.0, 10.0):
        maximo = precio + mult_max * atr_real
        actual = maximo   # peor caso: estamos justo en el maximo
        trade = TradeFalso(open_rate=precio, max_rate=maximo,
                           open_date_utc=df["date"].iloc[-1] + timedelta(hours=1))
        ratio = s.custom_stoploss(pair="BTC/USDT", trade=trade, current_time=AHORA,
                                  current_rate=actual, current_profit=0.0, after_fill=False)
        assert ratio < 0, f"stop no negativo con maximo a +{mult_max} x ATR"


def test_atr_del_stop_se_congela_en_la_entrada(estrategia):
    """El ATR usado por el stop se guarda al abrir y no cambia despues.

    Recalcularlo haria que una subida posterior de volatilidad alejara el stop
    de una posicion ya abierta, elevando su perdida maxima por encima del 0.5 %
    con el que se dimensiono.
    """
    precio, atr = 1000.0, 20.0
    df = estrategia.populate_indicators(construir_ohlcv_atr(300, precio, atr),
                                        {"pair": "BTC/USDT"})
    s = estrategia_con_datos(estrategia, df)
    atr_entrada = df["atr"].iloc[-1]

    trade = TradeFalso(open_rate=precio, open_date_utc=df["date"].iloc[-1] + timedelta(hours=1))
    primer_ratio = s.custom_stoploss(pair="BTC/USDT", trade=trade, current_time=AHORA,
                                     current_rate=precio, current_profit=0.0, after_fill=False)

    assert trade.get_custom_data("atr_entrada") == pytest.approx(atr_entrada)

    # El mercado se vuelve 5 veces mas volatil...
    df_volatil = estrategia.populate_indicators(
        construir_ohlcv_atr(300, precio, atr * 5), {"pair": "BTC/USDT"})
    s.dp._df = df_volatil

    segundo_ratio = s.custom_stoploss(pair="BTC/USDT", trade=trade, current_time=AHORA,
                                      current_rate=precio, current_profit=0.0, after_fill=False)
    assert segundo_ratio == pytest.approx(primer_ratio), (
        "el stop cambio al cambiar la volatilidad: el ATR no quedo congelado"
    )


def test_backstop_de_clase_es_mas_amplio_que_el_stop_por_atr(estrategia):
    """El stoploss fijo de la clase es solo una red: nunca debe activarse antes
    que el stop por ATR en condiciones normales."""
    assert estrategia.stoploss == R.STOPLOSS_BACKSTOP
    assert estrategia.stoploss < -0.10, (
        "el backstop debe ser mas amplio que el drawdown maximo permitido, "
        "para que el kill switch actue antes"
    )


# ===========================================================================
# Limite de posiciones simultaneas
# ===========================================================================

@pytest.mark.parametrize("abiertas,se_permite", [
    (0, True), (1, True), (2, True), (3, False), (4, False), (10, False),
])
def test_no_mas_de_tres_posiciones(estrategia, monkeypatch, abiertas, se_permite):
    """confirm_trade_entry corta la cuarta posicion aunque Freqtrade la deje pasar."""
    from freqtrade.persistence import Trade
    monkeypatch.setattr(Trade, "get_open_trade_count", staticmethod(lambda: abiertas))

    permitido = estrategia.confirm_trade_entry(
        pair="BTC/USDT", order_type="market", amount=1.0, rate=1000.0,
        time_in_force="GTC", current_time=AHORA, entry_tag="t", side="long",
    )
    assert permitido is se_permite


def test_no_se_permiten_cortos(estrategia, monkeypatch):
    """Spot no permite vender lo que no se tiene. Se rechaza en la ultima verja."""
    from freqtrade.persistence import Trade
    monkeypatch.setattr(Trade, "get_open_trade_count", staticmethod(lambda: 0))

    assert estrategia.confirm_trade_entry(
        pair="BTC/USDT", order_type="market", amount=1.0, rate=1000.0,
        time_in_force="GTC", current_time=AHORA, entry_tag="t", side="short",
    ) is False


def test_bot_start_recorta_max_open_trades_del_config(estrategia):
    """Un config con max_open_trades ilimitado se recorta al limite duro."""
    estrategia.config["max_open_trades"] = -1
    estrategia.bot_start()
    assert estrategia.config["max_open_trades"] == R.MAX_POSICIONES_SIMULTANEAS

    estrategia.config["max_open_trades"] = 25
    estrategia.bot_start()
    assert estrategia.config["max_open_trades"] == R.MAX_POSICIONES_SIMULTANEAS


# ===========================================================================
# Coherencia entre reglas
# ===========================================================================

def test_perdida_diaria_cubre_el_peor_dia_posible():
    """Si las 3 posiciones tocan stop el mismo dia, la perdida cabe en el limite
    diario. Si no cupiera, el limite diario nunca llegaria a dispararse."""
    peor_dia = R.RIESGO_POR_OPERACION * R.MAX_POSICIONES_SIMULTANEAS
    assert peor_dia < R.PERDIDA_DIARIA_MAXIMA
    assert peor_dia == pytest.approx(0.015)


def test_hacen_falta_muchas_perdidas_para_el_kill_switch():
    """Con 0.5 % por operacion, el kill switch del 10 % exige ~20 stops seguidos.

    Es la comprobacion de que el dimensionamiento y el cortacircuitos estan
    calibrados entre si: si bastaran 4 perdidas, el sistema no tendria margen
    para una racha mala normal.
    """
    operaciones = R.DRAWDOWN_TOTAL_MAXIMO / R.RIESGO_POR_OPERACION
    assert operaciones >= 15, (
        f"el kill switch saltaria tras solo {operaciones:.0f} perdidas seguidas"
    )


# ===========================================================================
# La variante optimizable no puede tocar el riesgo
# ===========================================================================

def test_la_variante_optimizable_solo_expone_parametros_de_senal():
    """BaselineTrendOpt abre la senal, nunca el riesgo.

    Es la frontera de seguridad del walk-forward. Si el riesgo por operacion o
    la distancia del stop entraran en el espacio de busqueda, el optimizador
    descubriria sin falta que arriesgar mas mejora el resultado del backtest —
    porque en el pasado ya sabemos que la cuenta no quebro. En el futuro no
    existe esa garantia.
    """
    from BaselineTrendOpt import BaselineTrendOpt

    estrategia = BaselineTrendOpt({
        "stake_currency": "USDT", "max_open_trades": 3, "dry_run": True,
        "timeframe": "1h", "runmode": "backtest", "exchange": {"name": "binance"},
    })
    estrategia.ft_load_hyper_params(False)   # sin esto el registro esta vacio
    optimizables = {nombre for nombre, _ in estrategia.enumerate_parameters()}

    assert optimizables == {"ema_rapida", "ema_lenta", "rsi_minimo", "rsi_maximo"}, (
        f"el espacio de busqueda cambio: {sorted(optimizables)}"
    )

    prohibidos = {"riesgo", "stop", "atr", "drawdown", "perdida", "posicion", "stake"}
    for nombre in optimizables:
        assert not any(p in nombre.lower() for p in prohibidos), (
            f"'{nombre}' parece un parametro de riesgo y esta abierto al optimizador"
        )


def test_la_variante_optimizable_hereda_las_reglas_de_riesgo_sin_tocarlas():
    """Los callbacks de riesgo son literalmente los mismos objetos."""
    from BaselineTrend import BaselineTrend
    from BaselineTrendOpt import BaselineTrendOpt

    assert issubclass(BaselineTrendOpt, BaselineTrend)
    for metodo in ("custom_stake_amount", "custom_stoploss", "confirm_trade_entry",
                   "_vela_cerrada_antes_de", "_atr_de_entrada"):
        assert getattr(BaselineTrendOpt, metodo) is getattr(BaselineTrend, metodo), (
            f"BaselineTrendOpt sobreescribe {metodo}: el riesgo dejaria de ser "
            "el mismo que el de la baseline y la comparacion no seria valida"
        )

    assert BaselineTrendOpt.stoploss == BaselineTrend.stoploss
    assert BaselineTrendOpt.startup_candle_count == BaselineTrend.startup_candle_count
    assert BaselineTrendOpt.can_short is False


@pytest.mark.parametrize("rapida,lenta,rsi_min,rsi_max,valida", [
    (20, 50, 40, 70, True),
    (50, 20, 40, 70, False),   # rapida mas lenta que la lenta
    (20, 50, 65, 70, False),   # banda de RSI demasiado estrecha
    (20, 20, 40, 70, False),   # medias iguales: no hay cruce posible
])
def test_la_variante_descarta_combinaciones_sin_sentido(rapida, lenta, rsi_min,
                                                        rsi_max, valida):
    """El optimizador propone al azar dentro de los rangos; algunas no valen.

    Sin este filtro, una combinacion con la EMA "rapida" mas lenta que la
    "lenta" produciria senales invertidas, y el optimizador podria quedarse con
    ella si por casualidad funciono en el tramo de entrenamiento.
    """
    from conftest import neutralizar_filtros, serie_con_cruce_alcista
    from BaselineTrendOpt import BaselineTrendOpt

    estrategia = BaselineTrendOpt({
        "stake_currency": "USDT", "max_open_trades": 3, "dry_run": True,
        "timeframe": "1h", "runmode": "backtest", "exchange": {"name": "binance"},
    })
    estrategia.ema_rapida.value = rapida
    estrategia.ema_lenta.value = lenta
    estrategia.rsi_minimo.value = rsi_min
    estrategia.rsi_maximo.value = rsi_max

    assert estrategia.combinacion_valida is valida

    if not valida:
        md = {"pair": "BTC/USDT"}
        df = neutralizar_filtros(
            estrategia.populate_indicators(serie_con_cruce_alcista(), md))
        df = estrategia.populate_entry_trend(df, md)
        assert int((df["enter_long"] == 1).sum()) == 0, (
            "una combinacion invalida genero senales de entrada"
        )


def test_el_detector_de_parametros_funciona_de_verdad():
    """Comprobacion del propio metodo de deteccion.

    Los dos tests anteriores afirman "esta estrategia no tiene parametros" y
    "esta otra tiene exactamente estos cuatro". Los dos serian verdad tambien si
    el detector estuviera roto y devolviera siempre vacio — que es justo lo que
    pasaba antes de llamar a `ft_load_hyper_params()`.

    Este test cierra el circulo: sobre una estrategia con un parametro conocido,
    el detector tiene que encontrarlo.
    """
    from freqtrade.strategy import IntParameter

    from BaselineTrend import BaselineTrend

    class ConParametro(BaselineTrend):
        canario = IntParameter(1, 10, default=5, space="buy", optimize=True)

    s = ConParametro({
        "stake_currency": "USDT", "max_open_trades": 3, "dry_run": True,
        "timeframe": "1h", "runmode": "backtest", "exchange": {"name": "binance"},
    })
    s.ft_load_hyper_params(False)

    assert "canario" in {n for n, _ in s.enumerate_parameters()}, (
        "el detector de parametros no encuentra un parametro que existe: los "
        "tests que dependen de el no estan comprobando nada"
    )
