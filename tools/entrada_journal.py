#!/usr/bin/env python3
"""
Genera la entrada semanal de docs/JOURNAL.md con los numeros REALES.

Por que existe
--------------
La parte de la bitacora que se rellena a mano es la que vale: que pasó, que te
sorprendio, que cambiarias. Pero copiar cifras del bot a una tabla es trabajo
mecanico, y el trabajo mecanico es justo lo que hace que la gente deje de
llevar bitacora a la tercera semana.

Este script rellena las tablas. Las preguntas las sigues contestando tu — se
dejan en blanco a proposito.

Los numeros salen de la base de datos del bot, nunca a mano. Una bitacora con
cifras inventadas o mal copiadas es peor que no tener bitacora: dentro de un
mes tomarias decisiones comparando contra algo que nunca ocurrio.

Uso:
    python tools/entrada_journal.py                    # semana actual
    python tools/entrada_journal.py --anadir           # la inserta en JOURNAL.md
    python tools/entrada_journal.py --desde 2026-09-01 --hasta 2026-09-08
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
JOURNAL = RAIZ / "docs" / "JOURNAL.md"
MARCA_ENTRADAS = "<!-- Las entradas nuevas van arriba, la más reciente primero. -->"


def leer_operaciones(db: Path, desde: datetime, hasta: datetime) -> list[dict]:
    if not db.exists():
        return []
    with sqlite3.connect(db) as con:
        con.row_factory = sqlite3.Row
        try:
            filas = con.execute(
                """
                SELECT pair, open_date, close_date, close_profit_abs AS profit_abs,
                       close_profit AS profit_ratio, exit_reason
                FROM trades
                WHERE is_open = 0 AND close_date IS NOT NULL
                ORDER BY close_date
                """).fetchall()
        except sqlite3.OperationalError:
            return []

    operaciones = []
    for f in filas:
        cierre = datetime.fromisoformat(f["close_date"]).replace(tzinfo=timezone.utc)
        if desde <= cierre < hasta:
            operaciones.append(dict(f) | {"cierre": cierre})
    return operaciones


def contar_abiertas(db: Path) -> int:
    if not db.exists():
        return 0
    with sqlite3.connect(db) as con:
        try:
            return con.execute("SELECT COUNT(*) FROM trades WHERE is_open = 1").fetchone()[0]
        except sqlite3.OperationalError:
            return 0


def acumulado(db: Path) -> tuple[int, float]:
    if not db.exists():
        return 0, 0.0
    with sqlite3.connect(db) as con:
        try:
            n, total = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(close_profit_abs), 0) "
                "FROM trades WHERE is_open = 0").fetchone()
            return n, total or 0.0
        except sqlite3.OperationalError:
            return 0, 0.0


def construir(desde: datetime, hasta: datetime, db: Path,
              capital: float, fase: str) -> str:
    ops = leer_operaciones(db, desde, hasta)
    abiertas = contar_abiertas(db)
    ops_totales, pnl_total = acumulado(db)

    ganadoras = [o for o in ops if o["profit_abs"] > 0]
    perdedoras = [o for o in ops if o["profit_abs"] < 0]
    pnl_semana = sum(o["profit_abs"] for o in ops)

    # Drawdown de la semana sobre el equity de cierre en cierre.
    equity = capital + pnl_total - pnl_semana
    pico = equity
    max_dd = 0.0
    for o in ops:
        equity += o["profit_abs"]
        pico = max(pico, equity)
        if pico > 0:
            max_dd = max(max_dd, (pico - equity) / pico * 100)

    L: list[str] = []
    a = L.append

    a(f"## Semana del {desde:%Y-%m-%d}")
    a("")
    a(f"**Fase:** {fase}")
    a(f"**Estado del bot:** _(¿corrió sin interrupciones? ¿cuántos reinicios?)_")
    a("")
    a("### Números")
    a("")
    a("| | |")
    a("|---|---|")
    a(f"| Operaciones cerradas | {len(ops)} |")
    a(f"| Ganadoras / perdedoras | {len(ganadoras)} / {len(perdedoras)} |")
    a(f"| P&L de la semana | {pnl_semana:+.2f} USDT ({pnl_semana / capital:+.2%}) |")
    a(f"| P&L acumulado | {pnl_total:+.2f} USDT ({pnl_total / capital:+.2%}) |")
    a(f"| Drawdown máximo esta semana | {max_dd:.2f} % |")
    a(f"| Equity actual | {capital + pnl_total:,.2f} USDT |")
    a(f"| Posiciones abiertas al cierre | {abiertas} |")
    a(f"| Operaciones desde el inicio | {ops_totales} |")
    a("")

    if ops:
        a("<details><summary>Operaciones de la semana</summary>")
        a("")
        a("| Par | Cierre | Resultado | Motivo de salida |")
        a("|---|---|---:|---|")
        for o in ops:
            a(f"| {o['pair']} | {o['cierre']:%Y-%m-%d %H:%M} | "
              f"{o['profit_ratio'] * 100:+.2f} % | {o['exit_reason'] or '—'} |")
        a("")
        a("</details>")
        a("")

    a("### Qué pasó")
    a("")
    if not ops and abiertas == 0:
        a("_(Cero operaciones. Con ~68 operaciones al año repartidas en 3 pares, "
          "una semana en blanco es lo normal: el filtro de la EMA(200) descarta la "
          "mayoría de los cruces. Si pasan 4 semanas sin ninguna, entonces sí toca "
          "mirar si algo está bloqueando las entradas.)_")
    else:
        a("_(Los hechos. Sin interpretación todavía.)_")
    a("")
    a("### Qué me sorprendió")
    a("")
    a("_(Lo que no esperabas. Si nada te sorprendió, escribe «nada» — pero "
      "piénsalo dos veces: lo que te sorprende es la diferencia entre tu modelo "
      "mental del sistema y lo que el sistema hace de verdad.)_")
    a("")
    a("### Qué cambiaría")
    a("")
    a("_(Ideas, NO cambios ejecutados. Durante el dry-run no se toca nada.)_")
    a("")
    a("### Cómo va contra el backtest")
    a("")
    a("```bash")
    a('python tools/report.py --backtest "$(ls -t user_data/backtest_results/*.zip '
      '| head -1)" --dry-run user_data/tradesv3.dryrun.sqlite')
    a("```")
    a("")
    if ops_totales < 30:
        a(f"_(Con {ops_totales} operaciones acumuladas la comparación todavía no "
          "significa nada. Por debajo de ~30 cualquier desviación es ruido.)_")
    else:
        a("_(Pega aquí la tabla de desviación. El criterio del plan es < 15 %.)_")
    a("")
    a("### Decisiones tomadas")
    a("")
    a("_(«Ninguna» es una respuesta válida y, durante el dry-run, la esperada.)_")
    a("")
    a("---")
    a("")
    return "\n".join(L)


def insertar_en_orden(texto: str, entrada: str, fecha: datetime) -> str:
    """Coloca la entrada donde le toca por fecha, no siempre arriba del todo.

    La bitacora va de mas reciente a mas antigua. Insertar siempre al principio
    funciona mientras cada entrada nueva sea la mas reciente — pero en cuanto se
    genera una semana atrasada (o la primera, que puede cubrir dias anteriores al
    arranque del bot), el orden se rompe en silencio y la bitacora deja de
    leerse cronologicamente.
    """
    cabeceras = list(re.finditer(r"^## Semana del (\d{4}-\d{2}-\d{2})",
                                 texto, re.MULTILINE))
    for m in cabeceras:
        if m.group(1) < f"{fecha:%Y-%m-%d}":
            # Primera entrada mas antigua: la nueva va justo antes.
            return texto[:m.start()] + entrada + texto[m.start():]

    # Mas antigua que todas, o no hay ninguna: detras de la marca.
    return texto.replace(MARCA_ENTRADAS, MARCA_ENTRADAS + "\n\n" + entrada, 1)


def main() -> int:
    p = argparse.ArgumentParser(description="Genera la entrada semanal del journal")
    p.add_argument("--desde", default=None, help="AAAA-MM-DD (por defecto, hace 7 dias)")
    p.add_argument("--hasta", default=None, help="AAAA-MM-DD (por defecto, hoy)")
    p.add_argument("--db", type=Path,
                   default=RAIZ / "user_data" / "tradesv3.dryrun.sqlite")
    p.add_argument("--capital", type=float, default=1000.0)
    p.add_argument("--fase", default="dry-run")
    p.add_argument("--anadir", action="store_true",
                   help="insertar la entrada en docs/JOURNAL.md, arriba del todo")
    args = p.parse_args()

    hasta = (datetime.fromisoformat(args.hasta) if args.hasta
             else datetime.combine(date.today(), datetime.min.time())
             + timedelta(days=1)).replace(tzinfo=timezone.utc)
    desde = (datetime.fromisoformat(args.desde).replace(tzinfo=timezone.utc)
             if args.desde else hasta - timedelta(days=7))

    # Copia fresca desde el volumen de Docker antes de componer nada: una
    # bitacora con numeros viejos es peor que una sin numeros.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from exportar_db import exportar
        exportar(args.db, silencioso=True)
    except ImportError:
        pass

    entrada = construir(desde, hasta, args.db, args.capital, args.fase)

    if not args.anadir:
        print(entrada)
        print("Para insertarla en docs/JOURNAL.md:  "
              "python tools/entrada_journal.py --anadir", file=sys.stderr)
        return 0

    texto = JOURNAL.read_text(encoding="utf-8")
    if MARCA_ENTRADAS not in texto:
        print(f"No se encontro la marca de insercion en {JOURNAL}.\n"
              "Pega la entrada a mano.", file=sys.stderr)
        print(entrada)
        return 1

    if f"## Semana del {desde:%Y-%m-%d}" in texto:
        print(f"Ya existe una entrada para la semana del {desde:%Y-%m-%d}.\n"
              "No se duplica. Si quieres regenerarla, borra la anterior a mano.",
              file=sys.stderr)
        return 2

    texto = insertar_en_orden(texto, entrada, desde)
    JOURNAL.write_text(texto, encoding="utf-8")
    print(f"Entrada de la semana del {desde:%Y-%m-%d} anadida a {JOURNAL.relative_to(RAIZ)}")
    print("Ahora rellena a mano las tres secciones en blanco. Son las que valen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
