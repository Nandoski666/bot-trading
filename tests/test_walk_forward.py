"""
Tests del walk-forward — sobre todo, de como se interpreta su resultado.

El riesgo aqui no es que el calculo este mal: es que un criterio de seguridad
de verde sobre un sistema roto. Estos tests fijan las dos propiedades que lo
impiden.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "tools"))

import walk_forward as wf  # noqa: E402


def ventana(i: int = 1) -> wf.Ventana:
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    return wf.Ventana(i, base, base, base, base)


def metricas(ops: int, ganado: float, perdido: float) -> dict:
    pf = ganado / perdido if perdido else (float("inf") if ganado else 0.0)
    return {"operaciones": ops, "bruto_ganado": ganado, "bruto_perdido": perdido,
            "profit_factor": pf, "beneficio_pct": 0.0, "win_rate": 0.0,
            "max_drawdown_pct": 0.0, "sharpe": 0.0, "calmar": 0.0,
            "expectativa_pct": 0.0, "curva_equity": []}


# ===========================================================================
# Generacion de ventanas
# ===========================================================================

def test_los_tramos_de_prueba_son_contiguos_y_no_se_solapan():
    """Encadenados forman una serie continua fuera de muestra.

    Si se solaparan, la curva de equity concatenada contaria dos veces las
    mismas operaciones y el resultado global estaria inflado.
    """
    ventanas = wf.generar_ventanas(
        datetime(2021, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 1, tzinfo=timezone.utc))
    assert len(ventanas) > 1

    for anterior, siguiente in zip(ventanas, ventanas[1:]):
        assert anterior.test_hasta == siguiente.test_desde, (
            "los tramos de prueba dejan un hueco o se solapan"
        )


def test_el_entrenamiento_siempre_precede_a_la_prueba():
    """Ninguna ventana entrena con datos posteriores a los que evalua.

    Es el lookahead en su version mas grosera: entrenar sobre el futuro del
    tramo de prueba. Barato de comprobar, catastrofico si ocurre.
    """
    for v in wf.generar_ventanas(datetime(2021, 1, 1, tzinfo=timezone.utc),
                                 datetime(2026, 1, 1, tzinfo=timezone.utc)):
        assert v.train_hasta <= v.test_desde
        assert v.train_desde < v.train_hasta < v.test_hasta


# ===========================================================================
# Degradacion agregada
# ===========================================================================

def test_la_agregada_no_se_deja_arrastrar_por_una_ventana_diminuta():
    """Una ventana de 2 operaciones no puede dominar el resultado global.

    Escenario real de este proyecto: una ventana de 3 operaciones produjo una
    degradacion de -25.825 %. La media simple quedo en -1.506 %, un numero sin
    ningun significado. La agregada pondera por operaciones y no por ventana.
    """
    resultados = [
        wf.Resultado(ventana=ventana(1),
                     train=metricas(200, 1000.0, 800.0),    # PF 1.25
                     test=metricas(60, 250.0, 250.0)),      # PF 1.00
        # Ventana diminuta con un cociente extremo.
        wf.Resultado(ventana=ventana(2),
                     train=metricas(3, 3.0, 100.0),         # PF 0.03
                     test=metricas(2, 200.0, 1.0)),         # PF 200
    ]

    media_simple = sum(r.degradacion for r in resultados) / 2
    agregada = wf.degradacion_agregada(resultados)

    assert agregada is not None
    assert abs(media_simple) > 100, "el escenario de prueba no es discriminante"
    assert abs(agregada["degradacion"]) < 1.0, (
        f"la agregada tambien se disparo: {agregada['degradacion']:.1%}"
    )
    assert agregada["ops_train"] == 203
    assert agregada["ops_test"] == 62


def test_la_agregada_suma_brutos_en_vez_de_promediar_cocientes():
    """PF agregado = suma de ganancias / suma de perdidas."""
    resultados = [
        wf.Resultado(ventana=ventana(1),
                     train=metricas(50, 300.0, 200.0),
                     test=metricas(20, 100.0, 100.0)),
        wf.Resultado(ventana=ventana(2),
                     train=metricas(50, 100.0, 200.0),
                     test=metricas(20, 50.0, 100.0)),
    ]
    a = wf.degradacion_agregada(resultados)
    assert a["pf_train"] == pytest.approx(400 / 400)   # 1.0
    assert a["pf_test"] == pytest.approx(150 / 200)    # 0.75
    assert a["degradacion"] == pytest.approx(0.25)


def test_sin_operaciones_de_entrenamiento_no_hay_degradacion():
    """Antes que inventar un numero, se devuelve None."""
    assert wf.degradacion_agregada([]) is None
    assert wf.degradacion_agregada([
        wf.Resultado(ventana=ventana(1),
                     train=metricas(0, 0.0, 0.0),
                     test=metricas(10, 50.0, 50.0))]) is None


# ===========================================================================
# El veredicto — la propiedad mas importante del ticket
# ===========================================================================

def _veredicto(resultados, tmp_path) -> tuple[float | None, bool, str]:
    destino = tmp_path / "reporte.md"
    principal, aprueba = wf.escribir_reporte(resultados, destino, epochs=10,
                                             loss="SharpeHyperOptLossDaily")
    return principal, aprueba, destino.read_text(encoding="utf-8")


def test_no_aprueba_si_el_entrenamiento_ya_pierde_dinero(tmp_path):
    """Degradacion baja sobre un sistema perdedor NO es una aprobacion.

    Es la trampa central de este criterio, y aparecio de verdad en este
    proyecto: profit factor 0.42 dentro de muestra y 0.36 fuera dan una
    degradacion del 14 %, por debajo del umbral del 40 %. Leido sin contexto,
    «pasa». En realidad el sistema pierde dinero en las dos muestras y la
    degradacion es baja unicamente porque no se puede caer mucho desde el suelo.

    Un umbral de seguridad que da verde sobre un sistema roto es peor que no
    tener umbral: transmite una confianza que no existe.
    """
    resultados = [wf.Resultado(ventana=ventana(1),
                               train=metricas(520, 420.0, 1000.0),   # PF 0.42
                               test=metricas(118, 360.0, 1000.0))]   # PF 0.36

    principal, aprueba, texto = _veredicto(resultados, tmp_path)

    assert principal < wf.UMBRAL_DEGRADACION, "el escenario no reproduce la trampa"
    assert aprueba is False, (
        "el walk-forward aprobo una estrategia que pierde dinero dentro y fuera "
        "de muestra, solo porque se degrada poco"
    )
    assert "NO APLICABLE" in texto
    assert "pierde dinero dentro y fuera de muestra" in texto


def test_aprueba_solo_con_entrenamiento_rentable_y_poca_degradacion(tmp_path):
    resultados = [wf.Resultado(ventana=ventana(1),
                               train=metricas(300, 1500.0, 1000.0),   # PF 1.50
                               test=metricas(80, 1300.0, 1000.0))]    # PF 1.30
    principal, aprueba, texto = _veredicto(resultados, tmp_path)

    assert principal == pytest.approx((1.5 - 1.3) / 1.5)
    assert aprueba is True
    assert "**PASA**" in texto


def test_no_aprueba_con_entrenamiento_rentable_pero_mucha_degradacion(tmp_path):
    """El caso clasico de sobreajuste: funciona en train, se cae en test."""
    resultados = [wf.Resultado(ventana=ventana(1),
                               train=metricas(300, 2000.0, 1000.0),   # PF 2.00
                               test=metricas(80, 900.0, 1000.0))]     # PF 0.90
    principal, aprueba, texto = _veredicto(resultados, tmp_path)

    assert principal == pytest.approx(0.55)
    assert aprueba is False
    assert "NO PASA" in texto
    assert "sobreajustada" in texto
