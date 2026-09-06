"""
Tests del filtro de contexto con IA.

Lo que se protege aqui no es que el filtro acierte —eso no se puede testear—
sino que **no pueda hacer dano**:

  * no toca el backtest, para que los resultados historicos sigan siendo
    reproducibles
  * solo puede restar operaciones, nunca provocarlas
  * falla abierto: si algo va mal, se opera

Un filtro de IA que pueda romper cualquiera de las tres deja de ser una ayuda y
pasa a ser una fuente de fallos silenciosos.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "user_data" / "strategies"))
sys.path.insert(0, str(RAIZ / "tools"))

from BaselineTrend import BaselineTrend  # noqa: E402


def estrategia_con(tmp_path: Path, runmode: str) -> BaselineTrend:
    return BaselineTrend({
        "stake_currency": "USDT", "max_open_trades": 3, "dry_run": True,
        "timeframe": "1h", "runmode": runmode, "exchange": {"name": "binance"},
        "user_data_dir": tmp_path,
    })


def escribir(tmp_path: Path, operar: bool, edad_horas: float = 0.0,
             vigencia: float = 6.0, motivo: str = "prueba") -> None:
    momento = datetime.now(timezone.utc) - timedelta(hours=edad_horas)
    (tmp_path / "decision_ia.json").write_text(json.dumps({
        "momento": momento.isoformat(timespec="seconds"),
        "vigencia_horas": vigencia,
        "operar": operar,
        "motivo": motivo,
        "senales_de_alerta": [],
    }), encoding="utf-8")


# ===========================================================================
# Lo mas importante: el backtest no se toca
# ===========================================================================

@pytest.mark.parametrize("modo", ["backtest", "hyperopt", "edge"])
def test_el_filtro_no_afecta_al_backtest(tmp_path, modo):
    """En backtest el veredicto se ignora aunque diga PAUSAR.

    Es la condicion que sostiene todo lo demas del proyecto. Un filtro no
    determinista metido en el backtest haria que dos ejecuciones sobre los
    mismos datos dieran resultados distintos, y a partir de ahi ninguna
    metrica historica significaria nada.
    """
    escribir(tmp_path, operar=False, motivo="pausar todo")
    s = estrategia_con(tmp_path, modo)

    permite, _ = s._ia_permite_operar()
    assert permite is True, (
        f"el filtro bloqueo en runmode={modo}: el backtest deja de ser "
        "reproducible")


@pytest.mark.parametrize("modo", ["dry_run", "live"])
def test_el_filtro_si_actua_en_operativa(tmp_path, modo):
    escribir(tmp_path, operar=False, motivo="volatilidad anomala")
    s = estrategia_con(tmp_path, modo)

    permite, motivo = s._ia_permite_operar()
    assert permite is False
    assert "volatilidad anomala" in motivo


# ===========================================================================
# Falla abierto
# ===========================================================================

def test_sin_archivo_se_opera(tmp_path):
    """Sin filtro configurado, el sistema funciona igual que antes."""
    s = estrategia_con(tmp_path, "dry_run")
    assert s._ia_permite_operar()[0] is True


def test_archivo_corrupto_se_opera(tmp_path):
    """Un JSON roto no puede paralizar el bot."""
    (tmp_path / "decision_ia.json").write_text("{esto no es json", encoding="utf-8")
    s = estrategia_con(tmp_path, "dry_run")
    assert s._ia_permite_operar()[0] is True


def test_decision_caducada_se_ignora(tmp_path):
    """Una opinion de hace 8 horas sobre el mercado de ahora no es informacion.

    Sin caducidad, un filtro que dijo PAUSAR y luego dejo de ejecutarse
    mantendria el sistema parado indefinidamente sin que nadie lo notara.
    """
    escribir(tmp_path, operar=False, edad_horas=8, vigencia=6)
    s = estrategia_con(tmp_path, "dry_run")
    assert s._ia_permite_operar()[0] is True


def test_decision_reciente_si_cuenta(tmp_path):
    escribir(tmp_path, operar=False, edad_horas=1, vigencia=6)
    s = estrategia_con(tmp_path, "dry_run")
    assert s._ia_permite_operar()[0] is False


def test_sin_clave_de_api_se_opera(tmp_path, monkeypatch):
    """Sin ANTHROPIC_API_KEY el filtro escribe 'operar', no 'pausar'."""
    import filtro_ia

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(filtro_ia, "DECISION", tmp_path / "decision_ia.json")
    monkeypatch.setattr(filtro_ia, "cargar_env", lambda *a, **k: {})

    veredicto, error = filtro_ia.consultar("mercado", "bots")
    assert veredicto is None
    assert "ANTHROPIC_API_KEY" in error

    decision = filtro_ia.escribir_decision(veredicto, error)
    assert decision["operar"] is True, (
        "sin clave de API el filtro bloqueo las entradas: deberia fallar abierto")


# ===========================================================================
# Solo puede restar
# ===========================================================================

def test_el_filtro_no_puede_provocar_entradas(tmp_path):
    """El veredicto no aparece en populate_entry_trend, solo en la confirmacion.

    Se comprueba sobre el arbol sintactico: si algun dia alguien intentara usar
    la salida del modelo para generar senales, el test lo detiene. El plan lo
    prohibe explicitamente — un LLM como generador de entradas no es
    backtesteable ni determinista.
    """
    import ast

    for archivo in (RAIZ / "user_data" / "strategies").glob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            if not (isinstance(nodo, ast.FunctionDef)
                    and nodo.name in ("populate_entry_trend", "populate_indicators")):
                continue
            fuente = ast.dump(nodo)
            assert "_ia_permite_operar" not in fuente, (
                f"{archivo.name}: {nodo.name} consulta el filtro de IA. El "
                "filtro solo puede vetar en confirm_trade_entry, nunca "
                "participar en la generacion de senales.")
            assert "decision_ia" not in fuente, (
                f"{archivo.name}: {nodo.name} lee la decision de la IA")


def test_el_veto_se_aplica_en_confirm_trade_entry(tmp_path, monkeypatch):
    """El veto llega de verdad hasta el rechazo de la orden."""
    from freqtrade.persistence import Trade

    monkeypatch.setattr(Trade, "get_open_trade_count", staticmethod(lambda: 0))
    escribir(tmp_path, operar=False, motivo="evento macro")
    s = estrategia_con(tmp_path, "dry_run")

    permitido = s.confirm_trade_entry(
        pair="BTC/USDT", order_type="market", amount=1.0, rate=1000.0,
        time_in_force="GTC", current_time=datetime.now(timezone.utc),
        entry_tag="t", side="long")
    assert permitido is False

    escribir(tmp_path, operar=True)
    assert s.confirm_trade_entry(
        pair="BTC/USDT", order_type="market", amount=1.0, rate=1000.0,
        time_in_force="GTC", current_time=datetime.now(timezone.utc),
        entry_tag="t", side="long") is True


def test_el_modelo_configurado_es_el_esperado():
    import filtro_ia
    assert filtro_ia.MODELO == "claude-opus-5"


# ===========================================================================
# Dos proveedores, mismas garantias
# ===========================================================================

def test_detecta_el_proveedor_por_la_forma_de_la_clave():
    """sk-ant- es Anthropic, gsk_ es Groq. Nada mas se acepta.

    Sin esta validacion, una cadena cualquiera acabaria en .env y el fallo
    aparecerira horas despues, en la primera consulta, con un error de API
    confuso.
    """
    import pegar_clave_ia as pk

    assert pk.detectar("sk-ant-" + "a" * 40) == "anthropic"
    assert pk.detectar("gsk_" + "B" * 40) == "groq"
    assert pk.detectar("no-es-una-clave") is None
    assert pk.detectar("") is None
    assert pk.detectar("sk-ant-corta") is None


def test_anthropic_gana_si_estan_las_dos_claves(monkeypatch):
    """Con ambas configuradas se usa Anthropic: mejor razonamiento.

    Que el orden sea explicito importa — si dependiera del orden del diccionario
    o del .env, el proveedor cambiaria sin que nadie lo hubiera decidido.
    """
    import filtro_ia

    monkeypatch.setattr(filtro_ia, "cargar_env", lambda *a, **k: {})
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    assert filtro_ia.proveedor_disponible() == "anthropic"

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert filtro_ia.proveedor_disponible() == "groq"

    monkeypatch.delenv("GROQ_API_KEY")
    assert filtro_ia.proveedor_disponible() is None


def test_sin_ninguna_clave_se_opera(tmp_path, monkeypatch):
    """La garantia de fallo abierto vale para los dos proveedores."""
    import filtro_ia

    monkeypatch.setattr(filtro_ia, "cargar_env", lambda *a, **k: {})
    monkeypatch.setattr(filtro_ia, "DECISION", tmp_path / "decision_ia.json")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    veredicto, error = filtro_ia.consultar("mercado", "bots")
    assert veredicto is None
    assert "GROQ_API_KEY" in error and "ANTHROPIC_API_KEY" in error

    decision = filtro_ia.escribir_decision(veredicto, error)
    assert decision["operar"] is True


def test_los_dos_proveedores_piden_salida_estructurada():
    """Ni Anthropic ni Groq deben devolver texto libre.

    El veredicto alimenta una decision automatica. Parsear prosa para decidir si
    se opera es exactamente la fragilidad que este proyecto evita en todo lo
    demas — y con dos proveedores distintos la tentacion de aflojar en uno de
    los dos es mayor.
    """
    import inspect

    import filtro_ia

    fuente = inspect.getsource(filtro_ia)
    assert "output_format=VeredictoIA" in fuente, "Anthropic sin esquema"
    assert '"type": "json_schema"' in fuente, "Groq sin esquema"
    assert '"strict": True' in fuente, "Groq sin modo estricto"
