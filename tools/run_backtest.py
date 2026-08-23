#!/usr/bin/env python3
"""
T5 — Backtest reproducible con costos realistas.

Un backtest sin comisiones ni slippage no es una estimacion optimista: es una
medicion de otra cosa. Con 0.15 % de coste por lado y una operacion media de
+0.5 %, los costos se llevan mas de la mitad del retorno bruto. Cualquier
metrica que se reporte aqui los incluye.

Que hace este script:
  1. fija la ventana temporal segun la fase del proyecto (in-sample / oos)
  2. impide tocar la ventana out-of-sample antes de tiempo (regla del plan)
  3. lanza el backtest con la comision efectiva correcta
  4. guarda un manifiesto con TODO lo necesario para reproducirlo identico:
     commit de git, version de Freqtrade, hash de los datos, comando exacto

Uso:
    python tools/run_backtest.py --window in-sample
    python tools/run_backtest.py --window out-of-sample --desbloquear-oos
    python tools/run_backtest.py --window custom --timerange 20220101-20230101
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

RAIZ = Path(__file__).resolve().parents[1]

# --- Costos de transaccion (seccion T5 del plan) ---------------------------
# Freqtrade no tiene un parametro de slippage: aplica una comision fija a cada
# lado de la operacion. El slippage se modela sumandolo a esa comision, que es
# la forma estandar y honesta de hacerlo. El efecto sobre el P&L es el mismo:
# se paga en cada entrada y en cada salida.
COMISION = 0.0010          # 0.10 % — taker de Binance spot
SLIPPAGE = 0.0005          # 0.05 % — deslizamiento estimado en ordenes a mercado
COMISION_EFECTIVA = COMISION + SLIPPAGE   # 0.15 % por lado -> 0.30 % ida y vuelta

# --- Ventanas temporales (seccion T5 del plan) -----------------------------
# El timerange de Freqtrade es [inicio, fin): el fin NO se incluye.
VENTANAS = {
    "in-sample": ("20210101-20240701",
                  "2021-01-01 a 2024-06-30 — desarrollo y ajuste"),
    "out-of-sample": ("20240701-",
                      "2024-07-01 a hoy — RESERVADA, no tocar antes de T6"),
    "full": ("20210101-",
             "todo el historico — solo para inspeccion, no para decidir"),
}

SEMILLA = 42   # ver nota_sobre_la_semilla()


def nota_sobre_la_semilla() -> str:
    return (
        "El backtest de Freqtrade es determinista: recorre las velas en orden y "
        "no muestrea nada al azar, asi que dos ejecuciones con los mismos datos "
        "y el mismo codigo dan exactamente el mismo resultado. La semilla se fija "
        "y se registra igualmente porque el hyperopt de T6 SI es estocastico, y "
        "conviene que toda la cadena use la misma."
    )


# ---------------------------------------------------------------------------
# Procedencia: sin esto, "reproducible" es una palabra
# ---------------------------------------------------------------------------

def commit_git() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=RAIZ,
                             capture_output=True, text=True, check=True).stdout.strip()
        sucio = subprocess.run(["git", "status", "--porcelain"], cwd=RAIZ,
                               capture_output=True, text=True, check=True).stdout.strip()
        return f"{sha}{' (con cambios sin commitear)' if sucio else ''}"
    except Exception:
        return "no disponible"


def version_freqtrade() -> str:
    try:
        import freqtrade
        return freqtrade.__version__
    except Exception:
        return "desconocida"


def huella_datos(datadir: Path, pares: list[str], timeframe: str) -> dict[str, str]:
    """SHA-256 de cada archivo de datos.

    Es lo que convierte "corri el backtest en enero" en algo verificable: si
    manana el resultado cambia, esta huella dice si cambiaron los datos o el
    codigo. Sin ella no hay forma de saberlo.
    """
    huellas = {}
    for par in pares:
        base = par.replace("/", "_")
        for candidato in (datadir / "binance" / f"{base}-{timeframe}.feather",
                          datadir / f"{base}-{timeframe}.feather"):
            if candidato.exists():
                h = hashlib.sha256(candidato.read_bytes()).hexdigest()
                huellas[par] = f"{h[:16]}… ({candidato.stat().st_size:,} bytes)"
                break
        else:
            huellas[par] = "ARCHIVO NO ENCONTRADO"
    return huellas


def leer_resumen(directorio: Path) -> dict | None:
    """Extrae los datos clave del ultimo resultado de backtest.

    Freqtrade guarda los resultados en un .zip cuyo nombre apunta desde
    `.last_result.json`. Se lee de ahi, y no de lo que creemos haber pedido,
    para que la verificacion sea independiente del comando.
    """
    import zipfile

    puntero = directorio / ".last_result.json"
    if not puntero.exists():
        return None
    try:
        nombre_zip = json.loads(puntero.read_text())["latest_backtest"]
        with zipfile.ZipFile(directorio / nombre_zip) as z:
            interno = next(n for n in z.namelist()
                           if n.endswith(".json") and "meta" not in n and "config" not in n)
            datos = json.loads(z.read(interno))
        est = next(iter(datos["strategy"].values()))
        return {
            "archivo": nombre_zip,
            "pares_del_resultado": est.get("pairlist", []),
            "max_open_trades": est.get("max_open_trades"),
            "operaciones": est.get("total_trades"),
            "beneficio_total_pct": round(est.get("profit_total", 0) * 100, 2),
            "max_drawdown_pct": round(est.get("max_drawdown_account", 0) * 100, 2),
        }
    except Exception as exc:      # noqa: BLE001 — la verificacion no debe romper el flujo
        print(f"(no se pudo leer el resumen del resultado: {exc})", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Backtest reproducible de BaselineTrend")
    p.add_argument("--window", choices=list(VENTANAS) + ["custom"], default="in-sample")
    p.add_argument("--timerange", default=None, help="solo con --window custom")
    p.add_argument("--strategy", default="BaselineTrend")
    p.add_argument("--config", default="user_data/config.dryrun.json")
    p.add_argument("--datadir", default="user_data/data")
    p.add_argument("--pares", nargs="*", default=["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "ADA/USDT", "AVAX/USDT", "LINK/USDT", "DOT/USDT", "POL/USDT", "LTC/USDT", "ATOM/USDT"])
    p.add_argument("--timeframe", default="1h")
    p.add_argument("--etiqueta", default=None, help="sufijo para el nombre del reporte")
    p.add_argument(
        "--desbloquear-oos", action="store_true",
        help="permite usar la ventana out-of-sample. Requiere haber completado T6.",
    )
    args = p.parse_args()

    # --- Guarda: la ventana out-of-sample esta cerrada hasta T6 -------------
    # No es burocracia. La ventana fuera de muestra solo sirve una vez: en el
    # momento en que se mira para decidir algo, deja de ser fuera de muestra y
    # pasa a ser parte del ajuste. Es un recurso que se gasta al usarlo.
    if args.window == "out-of-sample" and not args.desbloquear_oos:
        print(
            "BLOQUEADO: la ventana out-of-sample (2024-07-01 en adelante) esta\n"
            "reservada hasta completar T6 (walk-forward).\n\n"
            "Mirarla antes contamina la unica medicion honesta que queda del\n"
            "sistema: en cuanto se usa para decidir algo, deja de ser fuera de\n"
            "muestra. No se puede recuperar.\n\n"
            "Si T6 ya esta hecho y quieres gastarla, repite con --desbloquear-oos.",
            file=sys.stderr,
        )
        return 3

    if args.window == "custom":
        if not args.timerange:
            print("--window custom exige --timerange", file=sys.stderr)
            return 2
        timerange, descripcion = args.timerange, "ventana definida a mano"
    else:
        timerange, descripcion = VENTANAS[args.window]

    random.seed(SEMILLA)
    np.random.seed(SEMILLA)

    marca = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    etiqueta = args.etiqueta or args.window
    nombre = f"{args.strategy}_{etiqueta}_{marca}"

    binario = RAIZ / ".venv" / "bin" / "freqtrade"
    ejecutable = str(binario) if binario.exists() else "freqtrade"

    comando = [
        ejecutable, "backtesting",
        "--config", args.config,
        "--strategy", args.strategy,
        "--datadir", args.datadir,
        "--timeframe", args.timeframe,
        "--timerange", timerange,
        "--fee", str(COMISION_EFECTIVA),
        "--export", "signals",
        "--backtest-directory", "user_data/backtest_results",
        "--notes", nombre,
        "--breakdown", "month",
        "--cache", "none",          # nunca reutilizar un resultado cacheado
        # OJO: `--pairs` acepta varios valores en UNA sola bandera. Repetir la
        # bandera (--pairs A --pairs B) hace que argparse conserve solo la
        # ultima, y el backtest correria con un unico par sin decir nada. Se
        # detecto asi la primera vez: 87 operaciones en 3 anos y medio con
        # "Max open trades: 1", porque Freqtrade limita las posiciones
        # simultaneas al numero de pares del whitelist.
        "--pairs", *args.pares,
    ]

    print("=" * 78)
    print(f"Backtest {args.strategy} — ventana '{args.window}'")
    print("=" * 78)
    print(f"  periodo   : {timerange}  ({descripcion})")
    print(f"  comision  : {COMISION_EFECTIVA:.4%} por lado "
          f"= {COMISION:.2%} taker + {SLIPPAGE:.2%} slippage")
    print(f"  ida+vuelta: {2 * COMISION_EFECTIVA:.2%} del nocional")
    print(f"  pares     : {', '.join(args.pares)}")
    print(f"  semilla   : {SEMILLA}")
    print()

    (RAIZ / "user_data" / "backtest_results").mkdir(parents=True, exist_ok=True)

    resultado = subprocess.run(comando, cwd=RAIZ, text=True,
                               capture_output=True)
    salida = resultado.stdout + resultado.stderr
    print("\n".join(l for l in salida.splitlines() if " INFO - " not in l))

    # --- Manifiesto de reproducibilidad ------------------------------------
    manifiesto = {
        "generado_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ventana": args.window,
        "timerange": timerange,
        "descripcion_ventana": descripcion,
        "estrategia": args.strategy,
        "pares": args.pares,
        "timeframe": args.timeframe,
        "costos": {
            "comision_taker": COMISION,
            "slippage_estimado": SLIPPAGE,
            "comision_efectiva_por_lado": COMISION_EFECTIVA,
            "coste_ida_y_vuelta": 2 * COMISION_EFECTIVA,
            "como_se_modela": (
                "Freqtrade no tiene parametro de slippage; se suma a la comision "
                "via --fee, que se cobra en la entrada y en la salida."
            ),
        },
        "semilla": SEMILLA,
        "nota_semilla": nota_sobre_la_semilla(),
        "entorno": {
            "commit_git": commit_git(),
            "freqtrade": version_freqtrade(),
            "python": platform.python_version(),
            "plataforma": platform.platform(),
        },
        "datos": huella_datos(RAIZ / args.datadir, args.pares, args.timeframe),
        "comando_exacto": " ".join(comando),
        "codigo_salida": resultado.returncode,
    }

    ruta_manifiesto = RAIZ / "user_data" / "backtest_results" / f"{nombre}_manifiesto.json"

    if resultado.returncode != 0:
        print(f"\nEl backtest fallo (codigo {resultado.returncode}).", file=sys.stderr)
        ruta_manifiesto.write_text(json.dumps(manifiesto, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        return resultado.returncode

    # --- Verificacion de cordura sobre el resultado -------------------------
    # Un backtest que corre sin error pero sobre el universo equivocado es peor
    # que uno que falla: produce numeros con aspecto valido. Se comprueba contra
    # el propio archivo de resultados, no contra lo que creemos haber pedido.
    resumen = leer_resumen(RAIZ / "user_data" / "backtest_results")
    if resumen:
        manifiesto["resultado"] = resumen
        ruta_manifiesto.write_text(json.dumps(manifiesto, indent=2, ensure_ascii=False),
                                   encoding="utf-8")

        faltantes = set(args.pares) - set(resumen["pares_del_resultado"])
        if faltantes:
            print(f"\nAVISO: el backtest no cubrio {sorted(faltantes)}. "
                  "Revisa el whitelist y los datos descargados.", file=sys.stderr)
        if resumen["max_open_trades"] != 3:
            print(f"\nAVISO: max_open_trades efectivo = {resumen['max_open_trades']} "
                  "(deberia ser 3).", file=sys.stderr)
        if resumen["operaciones"] < 100:
            print(f"\nAVISO: solo {resumen['operaciones']} operaciones. El plan exige "
                  ">= 100 para que las metricas signifiquen algo.", file=sys.stderr)

    print("\n" + "=" * 78)
    if resumen:
        print(f"Resultados  : user_data/backtest_results/{resumen['archivo']}")
        print(f"  pares     : {', '.join(resumen['pares_del_resultado'])}")
        print(f"  operaciones: {resumen['operaciones']} | "
              f"beneficio: {resumen['beneficio_total_pct']:+.2f} % | "
              f"max DD: {resumen['max_drawdown_pct']:.2f} %")
    print(f"Manifiesto  : {ruta_manifiesto.relative_to(RAIZ)}")
    print()
    print("Para reproducir exactamente este backtest:")
    print(f"  {manifiesto['comando_exacto']}")
    print()
    print("Siguiente paso — tabla de metricas comparable:")
    print("  python tools/report.py --backtest user_data/backtest_results/"
          f"{resumen['archivo'] if resumen else '<archivo>.zip'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
