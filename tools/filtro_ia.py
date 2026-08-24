#!/usr/bin/env python3
"""
Filtro de contexto con IA — decide si HOY conviene operar, nunca QUE operar.

DONDE ENCAJA UN MODELO DE LENGUAJE Y DONDE NO
==============================================
Esta es la parte que mas facil es hacer mal, asi que conviene decirla antes que
el codigo.

Un LLM **no sirve** para generar senales de entrada:

  * *Latencia*: una decision tarda segundos. En 5 minutos de vela eso es una
    eternidad, y el precio con el que decidio ya no existe.
  * *No determinismo*: la misma pregunta puede dar respuestas distintas. Un
    backtest deja de ser reproducible, y sin backtest reproducible no hay
    forma de saber si el sistema funciona.
  * *No es backtesteable*: no se puede preguntar al modelo "que habrias dicho
    el 14 de marzo de 2023" sin contaminarlo con lo que ya sabe que paso.

Un LLM **si sirve** como filtro de contexto:

  * "¿Hay hoy un evento macro que aconseje no abrir posiciones?"
  * "¿El comportamiento reciente de los bots se parece a lo que se esperaba?"
  * "¿Hay algo anomalo en la estructura del mercado que un indicador no ve?"

Son preguntas lentas, cualitativas y de baja frecuencia. Ahi el lenguaje aporta
lo que un indicador no puede.

COMO SE INTEGRA SIN ROMPER LA VALIDACION
========================================
El filtro escribe su veredicto en `user_data/decision_ia.json`. Las estrategias
lo leen en `confirm_trade_entry` **solo en dry-run y live**. En backtest y en
hyperopt se ignora por completo.

Es una condicion innegociable: si el filtro afectara al backtest, ningun
resultado historico volveria a ser reproducible y todo el trabajo de validacion
del proyecto se caeria.

El filtro solo puede **restar** operaciones, nunca anadirlas. Puede vetar una
entrada que la estrategia queria hacer; no puede provocar una que la estrategia
no pidio. Esa asimetria es lo que lo mantiene dentro de lo auditable.

SIN CLAVE DE API
================
Si no hay `ANTHROPIC_API_KEY`, el filtro no bloquea nada y lo dice. Fallo
abierto y no cerrado: que se caiga la API de un tercero no puede dejar el
sistema sin operar, igual que un fallo de red no debe cerrar posiciones.

Uso:
    python tools/filtro_ia.py                 # una evaluacion
    python tools/filtro_ia.py --intervalo 3600  # bucle horario
    python tools/filtro_ia.py --explicar      # ver el veredicto actual
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, Field

RAIZ = Path(__file__).resolve().parents[1]
DECISION = RAIZ / "user_data" / "decision_ia.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_freqtrade import ClienteFreqtrade, ErrorAPI, cargar_env  # noqa: E402

MODELO = "claude-opus-5"

# Cuanto vale una decision antes de considerarse vieja. Si el filtro lleva mas
# de esto sin actualizarse, las estrategias lo ignoran: una opinion de ayer
# sobre el mercado de hoy no es informacion, es ruido con fecha.
VIGENCIA_HORAS = 6

BOTS = {"baseline": 8080, "orochi": 8081, "reversion": 8082,
        "ruptura": 8083, "momentum": 8084}


# ---------------------------------------------------------------------------
# Forma de la respuesta
# ---------------------------------------------------------------------------

class VeredictoIA(BaseModel):
    """Respuesta estructurada del modelo.

    Se usa un esquema y no texto libre a proposito: el resultado alimenta una
    decision automatica, y parsear prosa para decidir si se opera con dinero es
    exactamente el tipo de fragilidad que este proyecto evita en todo lo demas.
    """

    operar: bool = Field(
        description="true si no hay motivo de contexto para dejar de abrir "
                    "posiciones; false si conviene pausar")
    confianza: str = Field(
        description="alta, media o baja — cuanta certeza hay en el veredicto")
    motivo: str = Field(
        description="Una o dos frases, en espanol, explicando por que. "
                    "Concreto y verificable, sin adjetivos de relleno.")
    senales_de_alerta: list[str] = Field(
        default_factory=list,
        description="Hechos concretos observados que preocupan. Lista vacia si "
                    "no hay ninguno.")


INSTRUCCIONES = """Eres un filtro de contexto de un sistema de trading automatico de \
criptomonedas al contado. Tu unico trabajo es responder a una pregunta:

    ¿Hay alguna razon de CONTEXTO para que el sistema deje de abrir posiciones nuevas?

Lo que NO haces, y es importante:
- No eliges pares ni momentos de entrada. Las estrategias ya lo hacen con reglas
  deterministas que se han validado con anos de datos historicos.
- No predices precios. Si te ves opinando sobre si algo va a subir, te has salido
  de tu papel.
- No optimizas nada. No propones cambiar parametros ni reglas.

Solo puedes RESTAR operaciones, nunca anadirlas.

Recomienda pausar unicamente ante algo concreto y observable, por ejemplo:
- volatilidad muy fuera de lo normal en varios pares a la vez
- caidas coordinadas que sugieren un evento de mercado y no ruido
- que TODOS los bots esten perdiendo a la vez de forma inusual, lo que apunta a
  un cambio de regimen y no a la varianza normal de cada estrategia
- comportamiento incoherente entre bots que sugiere un fallo tecnico

NO recomiendes pausar por:
- que una estrategia pierda: se sabe que estas estrategias tienen esperanza
  negativa; perder es lo esperado, no una anomalia
- movimientos normales de mercado, por grandes que parezcan en un dia
- corazonadas, o "prudencia" sin un hecho detras

Ante la duda, deja operar. Un filtro que pausa a menudo acaba desactivado, y
entonces no protege de nada. Se te juzga por los pocos casos en que aciertas al
parar, no por parar mucho."""


# ---------------------------------------------------------------------------
# Recogida de contexto
# ---------------------------------------------------------------------------

def contexto_mercado(datadir: Path, pares: list[str]) -> str:
    """Resumen numerico del mercado reciente, calculado localmente.

    Se calcula aqui y no se le pide al modelo que lo deduzca: los numeros son
    trabajo de pandas, y el modelo aporta la lectura, no la aritmetica.
    """
    import pandas as pd

    lineas = []
    for par in pares:
        ruta = datadir / f"{par.replace('/', '_')}-1h.feather"
        if not ruta.exists():
            continue
        df = pd.read_feather(ruta).tail(200)
        if len(df) < 50:
            continue
        cierre = df["close"]
        var_24h = (cierre.iloc[-1] / cierre.iloc[-24] - 1) * 100
        var_7d = (cierre.iloc[-1] / cierre.iloc[0] - 1) * 100
        # Volatilidad de las ultimas 24 h frente a la de la semana: dice si
        # estamos en un momento anormalmente agitado.
        vol_reciente = cierre.pct_change().tail(24).std() * 100
        vol_semana = cierre.pct_change().std() * 100
        ratio = vol_reciente / vol_semana if vol_semana else 1.0
        lineas.append(
            f"  {par:<10} 24h {var_24h:+6.2f}%  7d {var_7d:+7.2f}%  "
            f"volatilidad reciente {ratio:.2f}x la de la semana")
    return "\n".join(lineas) if lineas else "  (sin datos de mercado)"


def contexto_bots() -> str:
    """Como le esta yendo a cada bot ahora mismo."""
    cargar_env()
    lineas = []
    for nombre, puerto in BOTS.items():
        try:
            c = ClienteFreqtrade(f"http://127.0.0.1:{puerto}", timeout=20)
            b = c.beneficio()
            abiertas = c.posiciones_abiertas()
            lineas.append(
                f"  {nombre:<10} {b.get('profit_closed_percent', 0):+6.2f}%  "
                f"{b.get('closed_trade_count', 0):>4} operaciones cerradas  "
                f"{len(abiertas)} abiertas")
        except ErrorAPI:
            lineas.append(f"  {nombre:<10} sin respuesta")
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Consulta
# ---------------------------------------------------------------------------

def consultar(mercado: str, bots: str) -> tuple[VeredictoIA | None, str | None]:
    """Devuelve (veredicto, error). Nunca lanza: un fallo aqui no puede parar el bot."""
    cargar_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, "sin ANTHROPIC_API_KEY"

    try:
        import anthropic
    except ImportError:
        return None, "falta el paquete anthropic (uv pip install anthropic)"

    pregunta = f"""Estado del mercado (ultimas 200 velas de 1h):
{mercado}

Estado de los bots (cada uno con 1.000 USDT simulados):
{bots}

Momento actual: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC

¿Hay alguna razon de contexto para dejar de abrir posiciones nuevas?"""

    try:
        cliente = anthropic.Anthropic()
        respuesta = cliente.with_options(timeout=90.0).messages.parse(
            model=MODELO,
            max_tokens=4096,
            system=INSTRUCCIONES,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": pregunta}],
            output_format=VeredictoIA,
        )
        if respuesta.stop_reason == "refusal":
            return None, "el modelo declino responder"
        return respuesta.parsed_output, None
    except Exception as exc:                      # noqa: BLE001
        # Cualquier fallo de red, cuota o formato: se reporta y se deja operar.
        return None, f"{type(exc).__name__}: {str(exc)[:200]}"


# ---------------------------------------------------------------------------
# Persistencia
# ---------------------------------------------------------------------------

def escribir_decision(veredicto: VeredictoIA | None, error: str | None) -> dict:
    decision = {
        "momento": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vigencia_horas": VIGENCIA_HORAS,
        "modelo": MODELO,
        # Fallo abierto: sin veredicto se opera. Que se caiga un servicio
        # externo no puede dejar el sistema sin gestionar sus posiciones.
        "operar": True if veredicto is None else veredicto.operar,
        "confianza": "n/a" if veredicto is None else veredicto.confianza,
        "motivo": (f"filtro no disponible ({error}); se deja operar"
                   if veredicto is None else veredicto.motivo),
        "senales_de_alerta": [] if veredicto is None else veredicto.senales_de_alerta,
        "error": error,
    }
    DECISION.parent.mkdir(parents=True, exist_ok=True)
    DECISION.write_text(json.dumps(decision, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    return decision


def leer_decision() -> dict | None:
    """Decision vigente, o None si no hay o esta caducada."""
    if not DECISION.exists():
        return None
    try:
        d = json.loads(DECISION.read_text(encoding="utf-8"))
        momento = datetime.fromisoformat(d["momento"])
        if datetime.now(timezone.utc) - momento > timedelta(hours=d.get(
                "vigencia_horas", VIGENCIA_HORAS)):
            return None
        return d
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


# ---------------------------------------------------------------------------

def evaluar(datadir: Path, pares: list[str], verboso: bool = True) -> dict:
    mercado = contexto_mercado(datadir, pares)
    bots = contexto_bots()

    if verboso:
        print("Mercado:")
        print(mercado)
        print("\nBots:")
        print(bots)
        print()

    veredicto, error = consultar(mercado, bots)
    decision = escribir_decision(veredicto, error)

    marca = "OPERAR" if decision["operar"] else "PAUSAR"
    print(f"[{decision['momento']}] {marca} "
          f"(confianza: {decision['confianza']})")
    print(f"  {decision['motivo']}")
    for s in decision["senales_de_alerta"]:
        print(f"  · {s}")
    if error:
        print(f"  (el filtro no pudo consultarse: {error})", file=sys.stderr)
    return decision


def main() -> int:
    p = argparse.ArgumentParser(description="Filtro de contexto con IA")
    p.add_argument("--datadir", type=Path, default=RAIZ / "user_data" / "data")
    p.add_argument("--pares", nargs="*", default=["BTC/USDT", "ETH/USDT", "SOL/USDT",
                                                  "BNB/USDT", "XRP/USDT"])
    p.add_argument("--intervalo", type=int, default=None,
                   help="segundos entre evaluaciones; sin esto hace una sola")
    p.add_argument("--explicar", action="store_true",
                   help="mostrar la decision vigente sin consultar")
    args = p.parse_args()

    if args.explicar:
        d = leer_decision()
        if d is None:
            print("No hay decision vigente (no existe o esta caducada).")
            print("Las estrategias operan sin filtro.")
            return 0
        print(json.dumps(d, indent=2, ensure_ascii=False))
        return 0

    if args.intervalo is None:
        evaluar(args.datadir, args.pares)
        return 0

    print(f"Filtro de contexto en marcha — una evaluacion cada "
          f"{args.intervalo} s ({args.intervalo/3600:.1f} h).")
    print("Solo puede vetar entradas, nunca provocarlas.\n")
    try:
        while True:
            try:
                evaluar(args.datadir, args.pares, verboso=False)
            except Exception as exc:              # noqa: BLE001
                print(f"  error en la evaluacion: {exc}", file=sys.stderr)
            print()
            time.sleep(args.intervalo)
    except KeyboardInterrupt:
        print("\nFiltro detenido.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
