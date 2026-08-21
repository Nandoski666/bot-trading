#!/usr/bin/env python3
"""
T7 — Reporte de metricas. Una sola fuente de verdad para comparar fases.

El problema
-----------
El backtest, el dry-run y el live producen sus numeros en formatos distintos y
con convenciones distintas. Comparados a ojo, siempre parecen coherentes.
Comparados de verdad, casi nunca lo son — y esa diferencia es exactamente la
senal que dice si el sistema se comporta en la realidad como se comporto en el
pasado simulado.

Este script calcula TODAS las metricas con el mismo codigo a partir de la lista
de operaciones, venga de donde venga. Si el backtest y el dry-run difieren, la
diferencia esta en el sistema, no en como se midio.

Uso:
    python tools/report.py --backtest user_data/backtest_results/backtest-result-*.zip
    python tools/report.py --backtest <zip> --dry-run user_data/tradesv3.dryrun.sqlite
    python tools/report.py --backtest <zip> --dry-run <db> --live <db> --salida informe.md
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

RAIZ = Path(__file__).resolve().parents[1]

# Criterios go/no-go de la seccion 4 del plan. Estan aqui, en el codigo que los
# evalua, para que el reporte no pueda "aprobar" con un umbral distinto al
# pactado.
CRITERIOS = {
    "operaciones_minimas": 100,
    "profit_factor_minimo": 1.20,
    "max_drawdown_maximo": 20.0,
    "sharpe_minimo": 1.00,
}

DIAS_POR_ANIO = 365


# ===========================================================================
# Modelo comun: una fase = una lista de operaciones
# ===========================================================================

@dataclass
class Fase:
    """Un conjunto de operaciones de una fase del proyecto."""
    nombre: str
    operaciones: pd.DataFrame     # columnas: pair, open_date, close_date, profit_abs, profit_ratio
    capital_inicial: float
    origen: str

    @property
    def vacia(self) -> bool:
        return self.operaciones.empty


# ---------------------------------------------------------------------------
# Lectores
# ---------------------------------------------------------------------------

def leer_backtest(ruta: Path) -> Fase:
    """Lee un resultado de backtest (.zip o .json de Freqtrade)."""
    if ruta.suffix == ".zip":
        with zipfile.ZipFile(ruta) as z:
            interno = next(n for n in z.namelist()
                           if n.endswith(".json") and "meta" not in n and "config" not in n)
            datos = json.loads(z.read(interno))
    else:
        datos = json.loads(ruta.read_text())

    est = next(iter(datos["strategy"].values()))
    df = pd.DataFrame(est["trades"])
    if not df.empty:
        df["open_date"] = pd.to_datetime(df["open_date"], utc=True)
        df["close_date"] = pd.to_datetime(df["close_date"], utc=True)

    return Fase(
        nombre="Backtest",
        operaciones=df,
        capital_inicial=est.get("starting_balance", 1000.0),
        origen=ruta.name,
    )


def leer_sqlite(ruta: Path, nombre: str, capital_inicial: float) -> Fase:
    """Lee las operaciones CERRADAS de la base de datos de Freqtrade.

    Las abiertas se excluyen a proposito: una posicion sin cerrar no tiene
    resultado, y contarla con su beneficio flotante inflaria las metricas justo
    en el sentido optimista.
    """
    with sqlite3.connect(ruta) as con:
        df = pd.read_sql_query(
            """
            SELECT pair, open_date, close_date, close_profit_abs AS profit_abs,
                   close_profit AS profit_ratio, exit_reason
            FROM trades
            WHERE is_open = 0 AND close_date IS NOT NULL
            ORDER BY close_date
            """, con)

    if not df.empty:
        df["open_date"] = pd.to_datetime(df["open_date"], utc=True)
        df["close_date"] = pd.to_datetime(df["close_date"], utc=True)
        df["profit_abs"] = df["profit_abs"].fillna(0.0)
        df["profit_ratio"] = df["profit_ratio"].fillna(0.0)

    return Fase(nombre=nombre, operaciones=df,
                capital_inicial=capital_inicial, origen=ruta.name)


# ===========================================================================
# Metricas — un unico calculo para todas las fases
# ===========================================================================

def calcular(fase: Fase) -> dict:
    df = fase.operaciones
    if df.empty:
        return {"operaciones": 0}

    ganadoras = df[df["profit_abs"] > 0]
    perdedoras = df[df["profit_abs"] < 0]

    bruto_ganado = ganadoras["profit_abs"].sum()
    bruto_perdido = -perdedoras["profit_abs"].sum()

    # Profit factor: cuanto se gana por cada unidad perdida. Es la metrica mas
    # dificil de maquillar — no depende del tamano de posicion ni del periodo.
    if bruto_perdido > 0:
        profit_factor = bruto_ganado / bruto_perdido
    elif bruto_ganado > 0:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0

    beneficio_total = df["profit_abs"].sum()
    equity = fase.capital_inicial + df["profit_abs"].cumsum()
    pico = equity.cummax()
    drawdown = (equity - pico) / pico
    max_dd = abs(drawdown.min()) * 100 if len(drawdown) else 0.0

    # --- Series diarias para Sharpe y Calmar -------------------------------
    # Se calcula sobre el equity diario, no por operacion: dos sistemas con el
    # mismo retorno pero distinta frecuencia no tienen el mismo Sharpe, y
    # anualizar por operacion lo distorsiona.
    serie = df.set_index("close_date")["profit_abs"].resample("1D").sum()
    equity_diario = fase.capital_inicial + serie.cumsum()
    retornos = equity_diario.pct_change().dropna()

    if len(retornos) > 1 and retornos.std() > 0:
        sharpe = (retornos.mean() / retornos.std()) * math.sqrt(DIAS_POR_ANIO)
    else:
        sharpe = 0.0

    dias = max((df["close_date"].max() - df["open_date"].min()).days, 1)
    retorno_total = beneficio_total / fase.capital_inicial
    # CAGR con proteccion: si se perdio todo, la formula no esta definida.
    if 1 + retorno_total > 0:
        cagr = ((1 + retorno_total) ** (DIAS_POR_ANIO / dias) - 1) * 100
    else:
        cagr = -100.0
    calmar = cagr / max_dd if max_dd > 0 else 0.0

    duracion = (df["close_date"] - df["open_date"]).dt.total_seconds() / 3600

    return {
        "operaciones": len(df),
        "ganadoras": len(ganadoras),
        "perdedoras": len(perdedoras),
        "win_rate": len(ganadoras) / len(df) * 100,
        "profit_factor": profit_factor,
        "beneficio_abs": beneficio_total,
        "beneficio_pct": retorno_total * 100,
        "cagr_pct": cagr,
        # Expectativa: lo que deja en promedio CADA operacion. Es el numero que
        # hay que multiplicar por el numero de operaciones futuras. Si es
        # negativo, ninguna gestion de capital lo arregla.
        "expectativa_abs": df["profit_abs"].mean(),
        "expectativa_pct": df["profit_ratio"].mean() * 100,
        "max_drawdown_pct": max_dd,
        "sharpe": sharpe,
        "calmar": calmar,
        "duracion_media_h": duracion.mean(),
        "mejor_op_pct": df["profit_ratio"].max() * 100,
        "peor_op_pct": df["profit_ratio"].min() * 100,
        "media_ganadora_abs": ganadoras["profit_abs"].mean() if len(ganadoras) else 0.0,
        "media_perdedora_abs": perdedoras["profit_abs"].mean() if len(perdedoras) else 0.0,
        "desde": df["open_date"].min(),
        "hasta": df["close_date"].max(),
        "dias": dias,
    }


# ===========================================================================
# Benchmark: comprar y mantener BTC
# ===========================================================================

def buy_and_hold_btc(desde: datetime, hasta: datetime, capital: float,
                     datadir: Path) -> dict | None:
    """Metricas de comprar BTC al principio y no tocar nada.

    Es el listón real. Un sistema que opera 200 veces, paga comisiones en cada
    una y termina por debajo de no hacer nada no esta aportando valor: esta
    convirtiendo tiempo y riesgo en comisiones.
    """
    # Freqtrade coloca los datos en <datadir>/<exchange>/ cuando se usa el
    # datadir por defecto, y directamente en <datadir>/ cuando se pasa
    # `--datadir` apuntando ya al directorio del exchange (que es lo que hace
    # download_data.sh). Se aceptan las dos disposiciones.
    candidatos = [
        datadir / "binance" / "BTC_USDT-1d.feather",
        datadir / "BTC_USDT-1d.feather",
        datadir / "binance" / "BTC_USDT-1h.feather",
        datadir / "BTC_USDT-1h.feather",
    ]
    ruta = next((c for c in candidatos if c.exists()), None)
    if ruta is None:
        return None

    df = pd.read_feather(ruta)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df[(df["date"] >= desde) & (df["date"] <= hasta)].reset_index(drop=True)
    if len(df) < 2:
        return None

    precio_ini, precio_fin = df["close"].iloc[0], df["close"].iloc[-1]
    unidades = capital / precio_ini
    equity = df["close"] * unidades

    pico = equity.cummax()
    max_dd = abs(((equity - pico) / pico).min()) * 100

    retorno = precio_fin / precio_ini - 1
    dias = max((df["date"].iloc[-1] - df["date"].iloc[0]).days, 1)
    cagr = ((1 + retorno) ** (DIAS_POR_ANIO / dias) - 1) * 100 if 1 + retorno > 0 else -100.0

    serie = equity.copy()
    serie.index = df["date"]
    retornos = serie.resample("1D").last().ffill().pct_change().dropna()
    sharpe = ((retornos.mean() / retornos.std()) * math.sqrt(DIAS_POR_ANIO)
              if len(retornos) > 1 and retornos.std() > 0 else 0.0)

    return {
        "beneficio_pct": retorno * 100,
        "beneficio_abs": capital * retorno,
        "cagr_pct": cagr,
        "max_drawdown_pct": max_dd,
        "sharpe": sharpe,
        "calmar": cagr / max_dd if max_dd > 0 else 0.0,
        "dias": dias,
    }


# ===========================================================================
# Salida en Markdown
# ===========================================================================

def f(valor, sufijo: str = "", dec: int = 2) -> str:
    if valor is None:
        return "—"
    if isinstance(valor, float):
        if math.isinf(valor):
            return "∞"
        if math.isnan(valor):
            return "—"
    if isinstance(valor, (int, float)):
        return f"{valor:,.{dec}f}{sufijo}"
    return str(valor)


def construir_reporte(fases: list[tuple[Fase, dict]], bh: dict | None,
                      capital: float) -> str:
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L: list[str] = []
    a = L.append

    a("# Reporte de metricas — BaselineTrend\n")
    a(f"*Generado: {ahora} — `python tools/report.py`*\n")
    a("> Todas las metricas incluyen **0.15 % de coste por lado** "
      "(0.10 % comision taker + 0.05 % slippage), es decir 0.30 % por operacion "
      "completa. Ningun numero de este reporte es bruto.\n")

    activas = [(fa, m) for fa, m in fases if m.get("operaciones", 0) > 0]
    if not activas:
        a("**No hay operaciones que reportar en ninguna fase.**\n")
        return "\n".join(L)

    # --- Tabla comparativa principal ---
    a("## Comparativa entre fases\n")
    cols = " | ".join(fa.nombre for fa, _ in activas)
    a(f"| Metrica | {cols} |")
    a("|---" * (len(activas) + 1) + "|")

    filas = [
        ("Operaciones",              lambda m: f(m["operaciones"], dec=0)),
        ("Ganadoras / perdedoras",   lambda m: f"{m['ganadoras']} / {m['perdedoras']}"),
        ("Win rate",                 lambda m: f(m["win_rate"], " %")),
        ("**Profit factor**",        lambda m: f"**{f(m['profit_factor'])}**"),
        ("Expectativa por operacion", lambda m: f"{f(m['expectativa_pct'], ' %')} "
                                                f"({f(m['expectativa_abs'], ' USDT')})"),
        ("Beneficio total",          lambda m: f"{f(m['beneficio_pct'], ' %')} "
                                                f"({f(m['beneficio_abs'], ' USDT')})"),
        ("CAGR",                     lambda m: f(m["cagr_pct"], " %")),
        ("**Max drawdown**",         lambda m: f"**{f(m['max_drawdown_pct'], ' %')}**"),
        ("Sharpe (anualizado)",      lambda m: f(m["sharpe"])),
        ("Calmar",                   lambda m: f(m["calmar"])),
        ("Duracion media",           lambda m: f"{f(m['duracion_media_h'], ' h', 1)}"),
        ("Mejor operacion",          lambda m: f(m["mejor_op_pct"], " %")),
        ("Peor operacion",           lambda m: f(m["peor_op_pct"], " %")),
        ("Media ganadora",           lambda m: f(m["media_ganadora_abs"], " USDT")),
        ("Media perdedora",          lambda m: f(m["media_perdedora_abs"], " USDT")),
        ("Periodo",                  lambda m: f"{m['desde']:%Y-%m-%d} → {m['hasta']:%Y-%m-%d}"),
        ("Dias cubiertos",           lambda m: f(m["dias"], dec=0)),
    ]
    for etiqueta, extraer in filas:
        a(f"| {etiqueta} | " + " | ".join(extraer(m) for _, m in activas) + " |")
    a("")

    # --- Benchmark ---
    a("## Contra comprar y mantener BTC\n")
    if bh is None:
        a("*(No hay datos de BTC/USDT para el periodo; ejecuta "
          "`./tools/download_data.sh`.)*\n")
    else:
        principal = activas[0]
        m = principal[1]
        a(f"Mismo periodo, mismo capital inicial ({f(capital, ' USDT')}), "
          "sin operar: comprar BTC el primer dia y no tocar nada.\n")
        a("| Metrica | " + principal[0].nombre + " | Buy & hold BTC | Diferencia |")
        a("|---|---:|---:|---:|")
        comparaciones = [
            ("Beneficio total", m["beneficio_pct"], bh["beneficio_pct"], " %"),
            ("CAGR", m["cagr_pct"], bh["cagr_pct"], " %"),
            ("Max drawdown", m["max_drawdown_pct"], bh["max_drawdown_pct"], " %"),
            ("Sharpe", m["sharpe"], bh["sharpe"], ""),
            ("**Calmar**", m["calmar"], bh["calmar"], ""),
        ]
        for etiqueta, propio, bench, sufijo in comparaciones:
            a(f"| {etiqueta} | {f(propio, sufijo)} | {f(bench, sufijo)} | "
              f"{f(propio - bench, sufijo)} |")
        a("")
        gana_calmar = m["calmar"] > bh["calmar"]
        a(f"**Criterio del plan — ganar a buy-and-hold en Calmar ratio: "
          f"{'CUMPLE' if gana_calmar else 'NO CUMPLE'}** "
          f"({f(m['calmar'])} vs {f(bh['calmar'])}).\n")
        a("> El Calmar (retorno anualizado / max drawdown) es la comparacion justa: "
          "premia el retorno por unidad de dolor, no el retorno a secas. Un sistema "
          "que gana menos que BTC pero con la mitad de drawdown puede ser mejor "
          "negocio; uno que gana menos y sufre mas, no.\n")

    # --- Criterios go/no-go ---
    a("## Criterios go/no-go (seccion 4 del plan)\n")
    fase_ref, m = activas[0]
    a(f"Evaluados sobre **{fase_ref.nombre}** ({fase_ref.origen}).\n")
    a("| Criterio | Umbral | Valor | Resultado |")
    a("|---|---|---:|---|")

    chequeos = [
        ("Numero de operaciones", f"≥ {CRITERIOS['operaciones_minimas']}",
         f(m["operaciones"], dec=0), m["operaciones"] >= CRITERIOS["operaciones_minimas"]),
        ("Profit factor (con costos)", f"> {CRITERIOS['profit_factor_minimo']}",
         f(m["profit_factor"]), m["profit_factor"] > CRITERIOS["profit_factor_minimo"]),
        ("Max drawdown", f"< {CRITERIOS['max_drawdown_maximo']} %",
         f(m["max_drawdown_pct"], " %"),
         m["max_drawdown_pct"] < CRITERIOS["max_drawdown_maximo"]),
        ("Sharpe anualizado", f"> {CRITERIOS['sharpe_minimo']}",
         f(m["sharpe"]), m["sharpe"] > CRITERIOS["sharpe_minimo"]),
    ]
    if bh is not None:
        chequeos.append(("Gana a buy-and-hold BTC en Calmar", f"> {f(bh['calmar'])}",
                         f(m["calmar"]), m["calmar"] > bh["calmar"]))

    for etiqueta, umbral, valor, pasa in chequeos:
        a(f"| {etiqueta} | {umbral} | {valor} | {'PASA' if pasa else '**FALLA**'} |")
    a("")
    a("| Criterio | Estado |")
    a("|---|---|")
    a("| Degradacion in-sample → out-of-sample < 40 % | ver "
      "`user_data/backtest_results/walk_forward/WALK_FORWARD_REPORT.md` (T6) |")
    a("| ≥ 4 semanas de dry-run con desviacion < 15 % | pendiente (T8) |")
    a("")

    fallos = [e for e, _, _, p in chequeos if not p]
    if fallos:
        a(f"**VEREDICTO: NO PASA A LIVE.** Fallan {len(fallos)} criterios: "
          + ", ".join(f"_{x}_" for x in fallos) + ".\n")
        a("El plan es explicito: si falla alguno, se vuelve a la fase de estrategia. "
          "**No se ajustan los criterios para que pase.**\n")
    else:
        a("**Todos los criterios medibles aqui se cumplen.** Faltan los dos que "
          "dependen de T6 y T8. Un backtest que pasa no es permiso para operar: "
          "es permiso para empezar el dry-run.\n")

    # --- Desviacion entre fases ---
    if len(activas) > 1:
        a("## Desviacion entre fases\n")
        a("El criterio del plan pide que el dry-run no se desvie mas de un 15 % del "
          "backtest del mismo periodo. Una desviacion grande casi siempre significa "
          "una de tres cosas: slippage real mayor que el simulado, ordenes que no se "
          "llenan al precio supuesto, o sesgo de anticipacion que el backtest no "
          "detecto.\n")
        base_fase, base_m = activas[0]
        a(f"| Metrica | {base_fase.nombre} | " +
          " | ".join(fa.nombre for fa, _ in activas[1:]) + " |")
        a("|---" * (len(activas) + 1) + "|")
        for clave, etiqueta in [("expectativa_pct", "Expectativa por operacion"),
                                ("win_rate", "Win rate"),
                                ("profit_factor", "Profit factor")]:
            fila = [f(base_m[clave])]
            for _, otra in activas[1:]:
                if base_m[clave] and not math.isinf(base_m[clave]):
                    desv = (otra[clave] - base_m[clave]) / abs(base_m[clave]) * 100
                    marca = "" if abs(desv) < 15 else " ⚠"
                    fila.append(f"{f(otra[clave])} ({desv:+.1f} %){marca}")
                else:
                    fila.append(f(otra[clave]))
            a(f"| {etiqueta} | " + " | ".join(fila) + " |")
        a("")

    # --- Aviso de resultados sospechosos ---
    sospechas = []
    if m["sharpe"] > 3:
        sospechas.append(f"Sharpe de {f(m['sharpe'])} — por encima de 3 en cripto spot "
                         "con 1h no es un hallazgo, es un sintoma")
    if 0 < m["max_drawdown_pct"] < 5 and m["operaciones"] > 50:
        sospechas.append(f"drawdown maximo de solo {f(m['max_drawdown_pct'], ' %')} "
                         f"en {m['operaciones']} operaciones")
    if m["win_rate"] > 75:
        sospechas.append(f"win rate del {f(m['win_rate'], ' %')}")
    if m["operaciones"] < CRITERIOS["operaciones_minimas"]:
        sospechas.append(f"solo {m['operaciones']} operaciones: por debajo de 100 las "
                         "metricas son ruido, en cualquier direccion")

    if sospechas:
        a("## Revisar antes de celebrar\n")
        a("El plan lo dice sin rodeos: si un resultado parece demasiado bueno, hay que "
          "asumir que hay un bug y buscarlo. Motivos de sospecha en este reporte:\n")
        for s in sospechas:
            a(f"- {s}")
        a("\nQue mirar primero: `freqtrade lookahead-analysis`, que la comision aplicada "
          "sea la correcta, y que el numero de pares del resultado sea el esperado.\n")

    return "\n".join(L)


# ===========================================================================

def main() -> int:
    p = argparse.ArgumentParser(description="Reporte comparable de metricas")
    p.add_argument("--backtest", type=Path, help="resultado de backtest (.zip o .json)")
    p.add_argument("--dry-run", type=Path, dest="dryrun", help="SQLite del dry-run")
    p.add_argument("--live", type=Path, help="SQLite del live")
    p.add_argument("--capital", type=float, default=1000.0)
    p.add_argument("--datadir", type=Path, default=RAIZ / "user_data" / "data")
    p.add_argument("--salida", type=Path, default=None)
    args = p.parse_args()

    if not any([args.backtest, args.dryrun, args.live]):
        p.error("hay que indicar al menos --backtest, --dry-run o --live")

    fases: list[Fase] = []
    if args.backtest:
        if not args.backtest.exists():
            print(f"No existe {args.backtest}")
            return 1
        fases.append(leer_backtest(args.backtest))
    if args.dryrun:
        # El SQLite vive en un volumen de Docker, no en el bind mount. Se saca
        # una copia fresca antes de leer: sin esto se reportarian los numeros de
        # la ultima vez que alguien lo exporto, sin ninguna senal de que estan
        # viejos.
        try:
            from exportar_db import exportar
            exportar(args.dryrun, silencioso=True)
        except ImportError:
            pass
        if args.dryrun.exists():
            fases.append(leer_sqlite(args.dryrun, "Dry-run", args.capital))
        else:
            print(f"(aviso: no existe {args.dryrun}, se omite el dry-run)")
    if args.live:
        if args.live.exists():
            fases.append(leer_sqlite(args.live, "Live", args.capital))
        else:
            print(f"(aviso: no existe {args.live}, se omite el live)")

    medidas = [(fa, calcular(fa)) for fa in fases]
    activas = [(fa, m) for fa, m in medidas if m.get("operaciones", 0) > 0]

    bh = None
    if activas:
        m = activas[0][1]
        bh = buy_and_hold_btc(m["desde"], m["hasta"],
                              activas[0][0].capital_inicial, args.datadir)

    capital = activas[0][0].capital_inicial if activas else args.capital
    texto = construir_reporte(medidas, bh, capital)

    if args.salida:
        args.salida.parent.mkdir(parents=True, exist_ok=True)
        args.salida.write_text(texto, encoding="utf-8")
        print(f"Reporte escrito en {args.salida}")
    else:
        print(texto)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
