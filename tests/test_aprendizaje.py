"""
Tests de la estrategia con aprendizaje automatico y del filtro de noticias.

Lo que se protege aqui es la misma propiedad de siempre: que el sistema siga
siendo **medible**. Un modelo de ML es la forma mas facil de romper eso sin que
se note — basta un dato del futuro colado en una feature para que el backtest
diga que todo funciona.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
DIR_ESTRATEGIAS = RAIZ / "user_data" / "strategies"
sys.path.insert(0, str(DIR_ESTRATEGIAS))
sys.path.insert(0, str(RAIZ / "tools"))

from EstrategiaBase import EstrategiaBase  # noqa: E402

ARCHIVO = DIR_ESTRATEGIAS / "AprendizModelo.py"


# ===========================================================================
# El modelo hereda el riesgo como todas las demas
# ===========================================================================

def test_el_modelo_hereda_el_riesgo_sin_tocarlo():
    """Que use IA no le da permiso para reescribir el dimensionamiento.

    Es donde mas tentacion hay de hacer excepciones ("el modelo sabe mas, que
    ajuste el tamano"). No: la ventaja predictiva y la gestion de riesgo son
    problemas separados, y mezclarlos hace que un fallo del modelo se convierta
    en un fallo de riesgo.
    """
    from AprendizModelo import AprendizModelo

    assert issubclass(AprendizModelo, EstrategiaBase)
    for metodo in ("custom_stake_amount", "custom_stoploss", "confirm_trade_entry",
                   "_vela_cerrada_antes_de", "_atr_de_entrada"):
        assert getattr(AprendizModelo, metodo) is getattr(EstrategiaBase, metodo), (
            f"AprendizModelo redefine {metodo}")
    assert AprendizModelo.can_short is False
    assert AprendizModelo.use_custom_stoploss is True


def test_declara_su_hipotesis():
    from AprendizModelo import AprendizModelo
    assert len(AprendizModelo.hipotesis) > 80


# ===========================================================================
# Sesgo de anticipacion: la unica excepcion permitida, y controlada
# ===========================================================================

def test_el_unico_shift_negativo_esta_en_la_etiqueta():
    """`set_freqai_targets` puede mirar al futuro; nada mas puede.

    La etiqueta de entrenamiento ES el futuro por definicion —se aprende a
    predecir el retorno de las proximas velas— y FreqAI garantiza que ninguna
    fila cuyo futuro no haya ocurrido entre en el entrenamiento.

    Pero esa excepcion tiene que estar acotada a ese metodo. Un shift negativo
    en una feature seria el futuro entrando por la puerta de atras, y el
    backtest lo aplaudiria.
    """
    arbol = ast.parse(ARCHIVO.read_text(encoding="utf-8"))

    def shifts_negativos(nodo):
        encontrados = []
        for n in ast.walk(nodo):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "shift"):
                for arg in list(n.args) + [k.value for k in n.keywords]:
                    if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
                        encontrados.append(n.lineno)
                    elif (isinstance(arg, ast.Constant)
                          and isinstance(arg.value, (int, float)) and arg.value < 0):
                        encontrados.append(n.lineno)
        return encontrados

    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.FunctionDef):
            continue
        negativos = shifts_negativos(nodo)
        if nodo.name == "set_freqai_targets":
            assert negativos, (
                "set_freqai_targets ya no mira hacia adelante: la etiqueta de "
                "entrenamiento deberia ser el retorno FUTURO")
        else:
            assert not negativos, (
                f"{nodo.name} usa shift negativo en la linea {negativos[0]}: "
                "una feature no puede leer el futuro")


def test_las_features_son_relativas_no_absolutas():
    """Las features deben ser comparables entre pares y entre epocas.

    Un modelo no puede aprender de "EMA = 78.432": ese numero no se repite
    nunca. Aprende de "el precio esta un 2 % sobre su EMA", que si es
    comparable. Es la diferencia entre un modelo que generaliza y uno que
    memoriza el rango de precios del periodo de entrenamiento.
    """
    texto = ARCHIVO.read_text(encoding="utf-8")
    arbol = ast.parse(texto)

    features = set()
    for nodo in ast.walk(arbol):
        if (isinstance(nodo, ast.Subscript) and isinstance(nodo.slice, ast.Constant)
                and isinstance(nodo.slice.value, str)
                and nodo.slice.value.startswith("%-")):
            features.add(nodo.slice.value)

    assert len(features) >= 8, f"solo se detectaron {len(features)} features"

    # Ninguna feature debe llamarse como un precio absoluto.
    prohibidas = {"%-close", "%-open", "%-high", "%-low", "%-ema", "%-sma", "%-precio"}
    assert not (features & prohibidas), (
        f"features con precios absolutos: {features & prohibidas}")


def test_la_configuracion_no_baraja_la_serie():
    """`shuffle: true` en una serie temporal mezcla futuro y pasado.

    Es el error clasico al aplicar ML a series de tiempo: el train_test_split
    por defecto baraja, y entonces el modelo se entrena con velas posteriores a
    las que evalua. El backtest sale espectacular y en vivo no funciona nada.
    """
    # La configuracion de FreqAI vive en su propio archivo: tenerla en el
    # principal impide correr estrategias con velas mayores que 5m, porque
    # Freqtrade valida include_timeframes contra el timeframe de la estrategia.
    cfg = json.loads((RAIZ / "user_data" / "config.freqai.json").read_text())
    fa = cfg.get("freqai", {})
    assert fa.get("data_split_parameters", {}).get("shuffle") is False, (
        "data_split_parameters.shuffle debe ser false en series temporales")


def test_las_secciones_que_van_a_sklearn_no_llevan_comentarios():
    """Las claves de comentario rompen el entrenamiento en silencio.

    `data_split_parameters` y `model_training_parameters` se pasan tal cual como
    kwargs a scikit-learn y al modelo. Una clave "//_comentario" provoca
    TypeError, cada entrenamiento falla, y el backtest termina sin error visible
    y con 0 operaciones — parece que la estrategia no encuentra oportunidades
    cuando en realidad nunca llego a entrenar.
    """
    cfg = json.loads((RAIZ / "user_data" / "config.freqai.json").read_text())
    for seccion in ("data_split_parameters", "model_training_parameters"):
        claves = list(cfg.get("freqai", {}).get(seccion, {}))
        comentarios = [k for k in claves if k.startswith("//")]
        assert not comentarios, (
            f"freqai.{seccion} tiene claves de comentario {comentarios}: se "
            "pasan como kwargs y abortan el entrenamiento")


def test_no_opera_fuera_del_dominio_del_modelo():
    """`do_predict == 1` tiene que estar en la condicion de entrada.

    Cuando el mercado entra en un regimen que el modelo no vio en su
    entrenamiento, FreqAI pone do_predict a 0. Operar igualmente es fiarse de
    una extrapolacion — un numero con decimales que el modelo se invento.
    """
    texto = ARCHIVO.read_text(encoding="utf-8")
    arbol = ast.parse(texto)
    entrada = next(n for n in ast.walk(arbol)
                   if isinstance(n, ast.FunctionDef) and n.name == "populate_entry_trend")
    assert "do_predict" in ast.dump(entrada), (
        "populate_entry_trend no comprueba do_predict")


# ===========================================================================
# Filtro de noticias
# ===========================================================================

def test_el_filtro_busca_noticias():
    import filtro_ia

    fuente = Path(filtro_ia.__file__).read_text(encoding="utf-8")
    assert "web_search" in fuente, "el filtro no tiene busqueda web activada"
    assert "max_uses" in fuente, (
        "la busqueda web sin max_uses puede disparar el coste: el filtro corre "
        "24 veces al dia")


def test_el_filtro_sigue_sin_poder_generar_entradas():
    """Anadir noticias no cambia la regla: el filtro solo puede vetar.

    Es facil que al ampliar el filtro se cuele en la generacion de senales
    ("si la noticia es buena, compra"). Eso lo haria no determinista y no
    backtesteable — exactamente lo que el plan prohibe.
    """
    for archivo in DIR_ESTRATEGIAS.glob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if (isinstance(nodo, ast.FunctionDef)
                    and nodo.name in ("populate_entry_trend", "populate_indicators",
                                      "set_freqai_targets",
                                      "feature_engineering_expand_all",
                                      "feature_engineering_standard")):
                volcado = ast.dump(nodo)
                assert "_ia_permite_operar" not in volcado, (
                    f"{archivo.name}:{nodo.name} consulta el filtro de IA")
                assert "decision_ia" not in volcado, (
                    f"{archivo.name}:{nodo.name} lee la decision de la IA")
                assert "eventos_encontrados" not in volcado, (
                    f"{archivo.name}:{nodo.name} usa noticias como senal")


def test_el_modelo_de_ia_es_el_esperado():
    import filtro_ia
    assert filtro_ia.MODELO == "claude-opus-5"
