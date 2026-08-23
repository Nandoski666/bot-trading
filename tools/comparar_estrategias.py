#!/usr/bin/env python3
"""
Compara las estrategias entre si sobre el mismo periodo y los mismos costes.

Por que no vale mirar los backtests por separado
------------------------------------------------
Cinco backtests corridos en momentos distintos, con universos o comisiones
distintas, no son comparables aunque lo parezcan. Este script lanza las cinco
en una sola ejecucion —mismo periodo, mismos pares, mismos costes— y saca una
tabla en la que la comparacion si significa algo.

Lo que mas informa no es cual gana, sino la relacion entre numero de
operaciones y resultado. Si la perdida crece con la actividad, el problema no
es la seleccion de senales: es que cada operacion tiene esperanza negativa y
operar mas solo acelera la sangria.

Uso:
    python tools/comparar_estrategias.py
    python tools/comparar_estrategias.py --timerange 20240701- --salida oos.md
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DESTINO = RAIZ / "user_data" / "backtest_results" / "comparativa"

FREQTRADE = str(RAIZ / ".venv" / "bin" / "freqtrade")
if not Path(FREQTRADE).exists():
    FREQTRADE = "freqtrade"

ESTRATEGIAS = ["BaselineTrend", "Orochi", "ReversionRSI",
               "RupturaDonchian", "MomentumMultiple"]

COMISION = 0.0010
SLIPPAGE = 0.0005
COSTE_LADO = COMISION + SLIPPAGE
COSTE_IDA_VUELTA = COSTE_LADO * 2


def correr(timerange: str, estrategias: list[str]) -> dict | None:
    DESTINO.mkdir(parents=True, exist_ok=True)
    comando = [
        FREQTRADE, "backtesting",
        "--config", "user_data/config.dryrun.json",
        "--strategy-list", *estrategias,
        "--datadir", "user_data/data",
        "--timerange", timerange,
        "--fee", str(COSTE_LADO),
        "--backtest-directory", str(DESTINO.relative_to(RAIZ)),
        "--cache", "none",
    ]
    print(f"Backtesting {len(estrategias)} estrategias en {timerange}…\n"
          f"  (comision efectiva {COSTE_LADO:.2%} por lado)\n")
    r = subprocess.run(comando, cwd=RAIZ, text=True, capture_output=True)
    if r.returncode != 0:
        print("\n".join((r.stdout + r.stderr).splitlines()[-20:]), file=sys.stderr)
        return None
    return leer_resultado()


def leer_resultado() -> dict | None:
    puntero = DESTINO / ".last_result.json"
    if not puntero.exists():
        return None
    nombre = json.loads(puntero.read_text())["latest_backtest"]
    with zipfile.ZipFile(DESTINO / nombre) as z:
        interno = next(n for n in z.namelist()
                       if n.endswith(".json") and "meta" not in n and "config" not in n)
        return json.loads(z.read(interno))


def metricas(est: dict) -> dict:
    ops = est.get("total_trades", 0)
    operaciones = est.get("trades", [])
    ganado = sum(t["profit_abs"] for t in operaciones if t["profit_abs"] > 0)
    perdido = -sum(t["profit_abs"] for t in operaciones if t["profit_abs"] < 0)

    if perdido > 0:
        pf = ganado / perdido
    elif ganado > 0:
        pf = float("inf")
    else:
        pf = 0.0

    neto_op = est.get("profit_mean", 0.0) * 100
    return {
        "operaciones": ops,
        "win_rate": (est.get("wins", 0) / ops * 100) if ops else 0.0,
        "profit_factor": pf,
        "neto_op": neto_op,
        # Lo que habria dejado cada operacion sin comisiones ni slippage. Es la
        # cifra que dice si la senal tiene algo o si solo esta pagando peaje.
        "bruto_op": neto_op + COSTE_IDA_VUELTA * 100,
        "beneficio": est.get("profit_total", 0.0) * 100,
        "max_dd": est.get("max_drawdown_account", 0.0) * 100,
        "sharpe": est.get("sharpe", 0.0),
        "duracion_h": est.get("holding_avg_s", 0) / 3600 if est.get("holding_avg_s") else 0,
    }


def f(v, sufijo="", dec=2):
    if v is None:
        return "—"
    if isinstance(v, float) and v == float("inf"):
        return "∞"
    return f"{v:,.{dec}f}{sufijo}"


def construir_reporte(datos: dict, timerange: str) -> str:
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    resultados = {n: metricas(e) for n, e in datos["strategy"].items()}
    activas = {n: m for n, m in resultados.items() if m["operaciones"] > 0}

    L: list[str] = []
    a = L.append
    a("# Comparativa de estrategias\n")
    a(f"*Generado: {ahora} — `python tools/comparar_estrategias.py`*\n")
    a(f"- Periodo: `{timerange}`")
    a(f"- Costes: **{COSTE_LADO:.2%} por lado** "
      f"({COMISION:.2%} comision taker + {SLIPPAGE:.2%} slippage) = "
      f"**{COSTE_IDA_VUELTA:.2%} por operacion completa**")
    a("- Todas comparten las mismas reglas de riesgo: 0.5 % por operacion, "
      "3 posiciones, stop de 2 x ATR\n")

    if not activas:
        a("**Ninguna estrategia produjo operaciones en este periodo.**\n")
        return "\n".join(L)

    a("## Resultados\n")
    a("| Estrategia | Ops | Win rate | Profit factor | Neto/op | Beneficio | Max DD |")
    a("|---|---:|---:|---:|---:|---:|---:|")
    for n, m in sorted(activas.items(), key=lambda x: -x[1]["beneficio"]):
        a(f"| `{n}` | {m['operaciones']:,} | {f(m['win_rate'], ' %')} "
          f"| {f(m['profit_factor'])} | {f(m['neto_op'], ' %', 3)} "
          f"| {f(m['beneficio'], ' %')} | {f(m['max_dd'], ' %')} |")
    a("")

    # --- Actividad contra resultado ---
    a("## Cuanto cuesta operar mas\n")
    a("| Estrategia | Ops | Beneficio | Neto/op | Coste/op | **Bruto/op** | Costes sobre la perdida |")
    a("|---|---:|---:|---:|---:|---:|---:|")
    for n, m in sorted(activas.items(), key=lambda x: x[1]["operaciones"]):
        parte = (COSTE_IDA_VUELTA * 100 / abs(m["neto_op"]) * 100
                 if m["neto_op"] < 0 else 0)
        a(f"| `{n}` | {m['operaciones']:,} | {f(m['beneficio'], ' %')} "
          f"| {f(m['neto_op'], ' %', 3)} | {COSTE_IDA_VUELTA * 100:.3f} % "
          f"| **{f(m['bruto_op'], ' %', 3)}** | {parte:.0f} % |")
    a("")
    a("**Bruto/op** es lo que habria dejado cada operacion en un mundo sin "
      "comisiones ni slippage. Es la prueba decisiva:\n")
    a("- Si el bruto es **positivo** y el neto negativo, la senal tiene algo y "
      "el problema son los costes: operar menos veces, o en un timeframe mayor, "
      "podria salvarla.")
    a("- Si el bruto tambien es **negativo**, la senal no tiene ninguna ventaja. "
      "Ninguna gestion de capital arregla eso: es una moneda sesgada en contra, "
      "y lanzarla mas veces solo acelera el resultado.\n")

    positivas = [n for n, m in activas.items() if m["bruto_op"] > 0]
    if positivas:
        a(f"Con bruto positivo: {', '.join(f'`{n}`' for n in positivas)}. "
          "Merecen una prueba con costes menores o menos frecuencia.\n")
    else:
        a("**Ninguna tiene bruto positivo.** No es un problema de comisiones: "
          "ninguna de estas senales predice nada en este periodo.\n")

    # --- Correlacion actividad/perdida ---
    xs = [m["operaciones"] for m in activas.values()]
    ys = [m["beneficio"] for m in activas.values()]
    if len(xs) > 2:
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
        den = ((sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5)
        if den:
            r = num / den
            a(f"**Correlacion entre numero de operaciones y beneficio: {r:+.3f}.** ")
            if r < -0.7:
                a("Fuertemente negativa: cuanto mas opera una estrategia, mas "
                  "pierde. Anadir actividad al sistema no anade oportunidades, "
                  "anade peaje.\n")
            a("")

    a("## Criterios go/no-go (seccion 4 del plan)\n")
    a("| Estrategia | ≥ 100 ops | PF > 1.2 | Max DD < 20 % | Sharpe > 1.0 | Veredicto |")
    a("|---|---|---|---|---|---|")
    for n, m in activas.items():
        c1 = m["operaciones"] >= 100
        c2 = m["profit_factor"] > 1.2
        c3 = m["max_dd"] < 20
        c4 = m["sharpe"] > 1.0
        marca = lambda b: "✅" if b else "❌"      # noqa: E731
        veredicto = "**PASA**" if all((c1, c2, c3, c4)) else "NO PASA"
        a(f"| `{n}` | {marca(c1)} | {marca(c2)} | {marca(c3)} | {marca(c4)} | {veredicto} |")
    a("")
    return "\n".join(L)


def main() -> int:
    p = argparse.ArgumentParser(description="Comparativa de estrategias")
    p.add_argument("--timerange", default="20210101-20240701")
    p.add_argument("--estrategias", nargs="*", default=ESTRATEGIAS)
    p.add_argument("--salida", type=Path,
                   default=DESTINO / "COMPARATIVA.md")
    p.add_argument("--solo-reporte", action="store_true",
                   help="regenerar desde el ultimo backtest guardado")
    args = p.parse_args()

    datos = leer_resultado() if args.solo_reporte else correr(args.timerange,
                                                              args.estrategias)
    if datos is None:
        print("No hay resultados que reportar.", file=sys.stderr)
        return 1

    texto = construir_reporte(datos, args.timerange)
    args.salida.parent.mkdir(parents=True, exist_ok=True)
    args.salida.write_text(texto, encoding="utf-8")
    print(texto)
    print(f"\nReporte escrito en {args.salida.relative_to(RAIZ)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
