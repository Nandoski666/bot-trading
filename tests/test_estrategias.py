"""
Tests que aplican a TODAS las estrategias por igual.

Al pasar de una estrategia a cinco, el riesgo deja de ser un problema de codigo
y pasa a ser un problema de gobierno: basta que una de las cinco redefina el
dimensionamiento o el stop —por descuido, o copiando de internet— para que las
reglas del plan dejen de valer sin que nadie se entere.

Estos tests recorren todas las estrategias del directorio automaticamente. Una
estrategia nueva queda cubierta el dia que se crea, sin tocar este archivo. Eso
es deliberado: un test que hay que acordarse de ampliar no protege de nada.
"""

from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
DIR_ESTRATEGIAS = RAIZ / "user_data" / "strategies"
sys.path.insert(0, str(DIR_ESTRATEGIAS))

import reglas_riesgo as R                       # noqa: E402
from EstrategiaBase import EstrategiaBase       # noqa: E402

CONFIG = {
    "stake_currency": "USDT", "stake_amount": "unlimited", "max_open_trades": 3,
    "dry_run": True, "timeframe": "1h", "runmode": "backtest",
    "exchange": {"name": "binance"},
}

# Metodos de riesgo que ninguna hija puede redefinir.
METODOS_DE_RIESGO = (
    "custom_stake_amount",
    "custom_stoploss",
    "confirm_trade_entry",
    "_vela_cerrada_antes_de",
    "_atr_de_entrada",
    "protections",
)

ATRIBUTOS_DE_RIESGO = ("stoploss", "use_custom_stoploss", "trailing_stop",
                       "can_short", "order_types")


def descubrir() -> list[type]:
    """Todas las estrategias del directorio, halladas por inspeccion."""
    clases = []
    for archivo in sorted(DIR_ESTRATEGIAS.glob("*.py")):
        if archivo.stem in ("__init__", "reglas_riesgo", "EstrategiaBase"):
            continue
        modulo = importlib.import_module(archivo.stem)
        for _, obj in inspect.getmembers(modulo, inspect.isclass):
            if (issubclass(obj, EstrategiaBase) and obj is not EstrategiaBase
                    and obj.__module__ == archivo.stem):
                clases.append(obj)
    return clases


ESTRATEGIAS = descubrir()
IDS = [c.__name__ for c in ESTRATEGIAS]


def test_se_descubrieron_las_estrategias():
    """Si esto falla, los tests parametrizados de abajo no prueban nada."""
    assert len(ESTRATEGIAS) >= 5, (
        f"solo se encontraron {len(ESTRATEGIAS)} estrategias: {IDS}"
    )
    assert "BaselineTrend" in IDS
    assert "Orochi" in IDS


# ===========================================================================
# El riesgo es intocable
# ===========================================================================

@pytest.mark.parametrize("estrategia", ESTRATEGIAS, ids=IDS)
@pytest.mark.parametrize("metodo", METODOS_DE_RIESGO)
def test_ninguna_estrategia_redefine_el_riesgo(estrategia, metodo):
    """Los callbacks de riesgo son literalmente los mismos objetos que en la base.

    No basta con que "hagan lo mismo": se compara identidad. Una hija que
    reimplemente `custom_stake_amount`, aunque hoy calcule igual, puede divergir
    manana sin que nadie lo note — y el dimensionamiento es lo que separa una
    racha mala de una cuenta vacia.
    """
    assert getattr(estrategia, metodo) is getattr(EstrategiaBase, metodo), (
        f"{estrategia.__name__} redefine {metodo}. El riesgo vive en "
        "EstrategiaBase y se hereda sin tocar."
    )


@pytest.mark.parametrize("estrategia", ESTRATEGIAS, ids=IDS)
@pytest.mark.parametrize("atributo", ATRIBUTOS_DE_RIESGO)
def test_ninguna_estrategia_cambia_los_ajustes_de_riesgo(estrategia, atributo):
    assert getattr(estrategia, atributo) == getattr(EstrategiaBase, atributo), (
        f"{estrategia.__name__} cambia {atributo}"
    )


@pytest.mark.parametrize("estrategia", ESTRATEGIAS, ids=IDS)
def test_solo_largos(estrategia):
    """Spot no permite vender lo que no se tiene."""
    assert estrategia.can_short is False


@pytest.mark.parametrize("estrategia", ESTRATEGIAS, ids=IDS)
def test_stop_por_atr_activo(estrategia):
    """El stop lo calcula custom_stoploss; el trailing nativo debe estar apagado.

    Dos mecanismos de trailing a la vez se pisan: el nativo mueve el stop en
    porcentaje fijo y el nuestro en unidades de ATR, y gana el que actue
    primero — que no es deterministico.
    """
    assert estrategia.use_custom_stoploss is True
    assert estrategia.trailing_stop is False
    assert estrategia.stoploss == R.STOPLOSS_BACKSTOP


@pytest.mark.parametrize("estrategia", ESTRATEGIAS, ids=IDS)
def test_calentamiento_suficiente(estrategia):
    """600 velas para que la EMA(200) converja de verdad."""
    assert estrategia.startup_candle_count >= 600


@pytest.mark.parametrize("estrategia", ESTRATEGIAS, ids=IDS)
def test_sin_objetivo_de_beneficio_fijo(estrategia):
    """Un ROI fijo cortaria las ganadoras largas, que son las que pagan."""
    assert min(estrategia.minimal_roi.values()) >= 1.0


# ===========================================================================
# Cada estrategia declara su hipotesis
# ===========================================================================

@pytest.mark.parametrize("estrategia", ESTRATEGIAS, ids=IDS)
def test_declara_su_hipotesis_de_mercado(estrategia):
    """El plan prohibe anadir indicadores sin justificar que hipotesis capturan.

    Obligar a escribir una frase no es burocracia: si no se puede explicar en
    una frase que se cree que hace el mercado, no se ha entendido la estrategia
    lo suficiente como para operarla — ni para saber cuando dejo de funcionar.
    """
    h = getattr(estrategia, "hipotesis", "SIN DECLARAR")
    assert h != "SIN DECLARAR", f"{estrategia.__name__} no declara hipotesis"
    assert len(h) > 80, (
        f"la hipotesis de {estrategia.__name__} tiene {len(h)} caracteres: "
        "es demasiado corta para decir algo"
    )


def test_las_hipotesis_son_distintas_entre_si():
    """Cinco estrategias con la misma hipotesis no son cinco estrategias.

    Correr varias solo aporta si capturan cosas distintas. Si todas apuestan a
    lo mismo, no hay diversificacion: hay una sola apuesta ejecutada cinco veces
    y pagando cinco veces las comisiones.

    Se comparan solo las estrategias *hermanas* —hijas directas de
    EstrategiaBase—. BaselineTrendOpt hereda de BaselineTrend y comparte su
    hipotesis a proposito: no es otra estrategia, es la misma con los parametros
    abiertos para medir el sobreajuste en el walk-forward.
    """
    hermanas = [c for c in ESTRATEGIAS if EstrategiaBase in c.__bases__]
    assert len(hermanas) >= 5, f"solo {len(hermanas)} estrategias independientes"

    hipotesis = [c.hipotesis for c in hermanas]
    repetidas = {h for h in hipotesis if hipotesis.count(h) > 1}
    assert not repetidas, (
        "hay hipotesis repetidas entre estrategias independientes: "
        + "; ".join(sorted(h[:60] for h in repetidas))
    )


# ===========================================================================
# Sin lookahead
# ===========================================================================

@pytest.mark.parametrize("estrategia", ESTRATEGIAS, ids=IDS)
def test_sin_desplazamientos_negativos(estrategia):
    """Ningun `.shift()` con argumento negativo: es traerse el futuro."""
    import ast

    archivo = DIR_ESTRATEGIAS / f"{estrategia.__module__}.py"
    arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
    for nodo in ast.walk(arbol):
        if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
                and nodo.func.attr == "shift"):
            continue
        for arg in list(nodo.args) + [k.value for k in nodo.keywords
                                      if k.arg in (None, "periods")]:
            negativo = (isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub)) or (
                isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float))
                and arg.value < 0)
            assert not negativo, (
                f"{archivo.name}:{nodo.lineno} usa .shift() negativo"
            )


@pytest.mark.parametrize("estrategia", ESTRATEGIAS, ids=IDS)
def test_sin_relleno_hacia_atras(estrategia):
    """Ningun `bfill()` ni `fillna(method='bfill')`.

    Rellenar hacia atras copia un valor futuro sobre una fila pasada. Es el
    error mas silencioso de todos: no rompe nada, no avisa, y mejora el
    backtest.
    """
    texto = (DIR_ESTRATEGIAS / f"{estrategia.__module__}.py").read_text(encoding="utf-8")
    import ast
    arbol = ast.parse(texto)
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute):
            assert nodo.func.attr != "bfill", (
                f"{estrategia.__name__} usa bfill(): copia datos del futuro "
                "hacia el pasado"
            )
            if nodo.func.attr == "fillna":
                for k in nodo.keywords:
                    if k.arg == "method" and isinstance(k.value, ast.Constant):
                        assert k.value.value != "bfill", (
                            f"{estrategia.__name__} usa fillna(method='bfill')"
                        )


@pytest.mark.parametrize("estrategia", ESTRATEGIAS, ids=IDS)
def test_los_indicadores_no_cambian_al_llegar_el_futuro(estrategia):
    """Truncar la serie no altera los valores ya calculados.

    Es la definicion operativa de "no mira al futuro": el valor de la vela N
    depende solo de las velas 0..N. Si al anadir 50 velas cambian los
    indicadores de antes, hay una dependencia del futuro.
    """
    import numpy as np
    from conftest import serie_con_cruce_alcista

    s = estrategia(CONFIG)
    md = {"pair": "BTC/USDT"}

    completo = serie_con_cruce_alcista()
    corte = len(completo) - 50

    ind_completo = s.populate_indicators(completo.copy(), md)
    ind_truncado = s.populate_indicators(
        completo.iloc[:corte].copy().reset_index(drop=True), md)

    columnas = [c for c in ind_truncado.columns
                if c not in ("date", "open", "high", "low", "close", "volume")
                and ind_truncado[c].dtype.kind in "fiu"]
    assert columnas, f"{estrategia.__name__} no produjo indicadores numericos"

    for col in columnas:
        a = ind_completo[col].iloc[:corte].to_numpy(dtype=float)
        b = ind_truncado[col].to_numpy(dtype=float)
        validos = ~(np.isnan(a) | np.isnan(b))
        if validos.sum() < 20:
            continue
        np.testing.assert_allclose(
            a[validos], b[validos], rtol=1e-9,
            err_msg=(f"{estrategia.__name__}: '{col}' cambia en el pasado al "
                     "anadir velas futuras"))


# ===========================================================================
# Las senales son coherentes
# ===========================================================================

@pytest.mark.parametrize("estrategia", ESTRATEGIAS, ids=IDS)
def test_entrada_y_salida_nunca_coinciden(estrategia):
    from conftest import serie_con_cruce_alcista

    s = estrategia(CONFIG)
    md = {"pair": "BTC/USDT"}
    df = s.populate_indicators(serie_con_cruce_alcista(), md)
    df = s.populate_exit_trend(s.populate_entry_trend(df, md), md)

    if "enter_long" in df.columns and "exit_long" in df.columns:
        ambas = df[(df["enter_long"] == 1) & (df["exit_long"] == 1)]
        assert ambas.empty, f"{estrategia.__name__}: {len(ambas)} velas con ambas senales"


@pytest.mark.parametrize("estrategia", ESTRATEGIAS, ids=IDS)
def test_no_opera_sin_volumen(estrategia):
    """Volumen 0 es un hueco de datos, no un mercado."""
    from conftest import serie_con_cruce_alcista

    s = estrategia(CONFIG)
    md = {"pair": "BTC/USDT"}
    df = s.populate_indicators(serie_con_cruce_alcista(), md)
    df["volume"] = 0.0
    df = s.populate_entry_trend(df, md)

    if "enter_long" in df.columns:
        assert int((df["enter_long"] == 1).sum()) == 0, (
            f"{estrategia.__name__} genero senales sobre velas sin volumen")


# ===========================================================================
# El config no puede pisar el timeframe de la estrategia
# ===========================================================================

def test_el_config_no_fija_el_timeframe():
    """`timeframe` en config.json anula el de la estrategia, en silencio.

    Freqtrade da prioridad al config sobre los atributos de clase. Con
    `"timeframe": "1h"` puesto, las variantes *Rapida —que declaran 5m— corrian
    en realidad a 1 hora, y nada lo avisaba: FreqUI mostraba "1h" junto al
    nombre de la estrategia rapida y ahi se detecto.

    Es peor que un error normal porque no falla: el sistema funciona, opera, y
    mide otra cosa distinta de la que uno cree estar midiendo.
    """
    import json

    for archivo in ("config.dryrun.json", "config.live.json"):
        ruta = RAIZ / "user_data" / archivo
        if not ruta.exists():
            continue
        cfg = json.loads(ruta.read_text(encoding="utf-8"))
        assert "timeframe" not in cfg, (
            f"{archivo} fija 'timeframe': anula el de cada estrategia sin avisar")


@pytest.mark.parametrize("estrategia", ESTRATEGIAS, ids=IDS)
def test_cada_estrategia_declara_su_timeframe(estrategia):
    assert estrategia.timeframe in ("5m", "15m", "1h", "4h"), (
        f"{estrategia.__name__} usa un timeframe inesperado: {estrategia.timeframe}")


def test_las_variantes_rapidas_son_de_cinco_minutos():
    rapidas = [c for c in ESTRATEGIAS if c.__name__.endswith("Rapida")]
    assert len(rapidas) == 5, f"se encontraron {len(rapidas)} variantes rapidas"
    for c in rapidas:
        assert c.timeframe == "5m", f"{c.__name__} no esta en 5m sino en {c.timeframe}"
