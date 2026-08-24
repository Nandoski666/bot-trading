#!/usr/bin/env python3
"""
Que estan haciendo los bots ahora mismo.

Con cinco estrategias corriendo a la vez, la pregunta deja de ser "¿funciona?" y
pasa a ser "¿cual esta haciendo que?". Este script responde las dos:

  * si cada bot esta vivo y procesando velas
  * que posiciones tiene abiertas y como van
  * cuantas senales de entrada ha producido su estrategia estos ultimos dias

La ultima columna es la util para diagnosticar silencio. Una estrategia que
lleva 200 velas sin generar una sola senal esta esperando (o mal calibrada); una
que genera veinte al dia y no tiene posiciones esta topando con el limite de 3
simultaneas o con las protecciones.

Uso:
    python tools/estado.py
    python tools/estado.py --bot orochi
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api_freqtrade import ClienteFreqtrade, ErrorAPI  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ / "user_data" / "strategies"))
from reglas_riesgo import (  # noqa: E402
    DRAWDOWN_TOTAL_MAXIMO,
    MAX_POSICIONES_SIMULTANEAS,
    PERDIDA_DIARIA_MAXIMA,
    RIESGO_POR_OPERACION,
)

VERDE, ROJO, AMARILLO, GRIS, FIN = (
    "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m")

# nombre -> puerto en el host (ver docker-compose.yml)
BOTS = {
    "baseline":  8080,
    "orochi":    8081,
    "reversion": 8082,
    "ruptura":   8083,
    "momentum":  8084,
}

VELAS_SENALES = 200   # ventana para contar senales recientes


def senales_recientes(cliente: ClienteFreqtrade, par: str) -> tuple[int, str | None]:
    """Cuantas senales de entrada produjo la estrategia en las ultimas velas.

    Se lee del dataframe que el propio bot tiene analizado, asi que refleja
    exactamente lo que decidio — no un calculo paralelo que podria diferir.
    """
    datos = cliente._peticion("GET", "pair_candles",
                              params={"pair": par, "timeframe": "1h",
                                      "limit": VELAS_SENALES})
    columnas = datos.get("columns", [])
    filas = datos.get("data", [])
    if "enter_long" not in columnas or not filas:
        return 0, None

    i_entrada = columnas.index("enter_long")
    i_fecha = columnas.index("date") if "date" in columnas else None

    total = 0
    ultima = None
    for fila in filas:
        if fila[i_entrada]:
            total += 1
            if i_fecha is not None:
                ultima = fila[i_fecha]
    return total, ultima


def estado_latido(vivo: bool, silencio: float | None) -> str:
    """Sufijo que describe el latido, incluido el caso de arranque en curso."""
    if silencio is None:
        return f"  {AMARILLO}← arrancando{FIN}"
    if not vivo:
        return f"  {ROJO}← sin latir ({silencio / 60:.0f} min){FIN}"
    return ""


def revisar(nombre: str, puerto: int, detalle: bool) -> dict | None:
    # Timeout generoso: al arrancar, el bot descarga velas de 12 pares antes de
    # atender la API, y 15 s no siempre bastan.
    cliente = ClienteFreqtrade(f"http://127.0.0.1:{puerto}", timeout=45)
    try:
        salud = cliente.salud()
        config = cliente.estado_bot()
        balance = cliente.balance()
        abiertas = cliente.posiciones_abiertas()
        beneficio = cliente.beneficio()
    except ErrorAPI as exc:
        print(f"  {ROJO}{nombre:<11}{FIN} sin respuesta — {str(exc)[:60]}")
        return None

    # Recien arrancado, el bot responde a la API pero todavia no ha completado un
    # ciclo: `last_process` viene a None. Antes esto reventaba el script con
    # "Invalid isoformat string: 'None'" — justo en el momento en que uno mira el
    # estado, que es despues de reiniciar.
    marca = salud.get("last_process")
    if marca in (None, "None", ""):
        silencio = None
        vivo = True          # responde; solo esta calentando
    else:
        ultimo = datetime.fromisoformat(str(marca).replace("Z", "+00:00"))
        silencio = (datetime.now(timezone.utc) - ultimo).total_seconds()
        vivo = silencio < 120

    equity = balance.get("total", 0)
    estrategia = config.get("strategy", "?")
    cerradas = beneficio.get("closed_trade_count", 0)
    pnl = beneficio.get("profit_closed_percent", 0)

    color_pnl = VERDE if pnl > 0 else (ROJO if pnl < 0 else GRIS)
    print(f"  {VERDE if vivo else ROJO}●{FIN} {nombre:<11}{GRIS}{estrategia:<18}{FIN}"
          f"{equity:>10,.2f}  {color_pnl}{pnl:>+7.2f} %{FIN}"
          f"{cerradas:>6} ops  {len(abiertas)}/{MAX_POSICIONES_SIMULTANEAS} abiertas"
          f"{estado_latido(vivo, silencio)}")

    for t in abiertas:
        p = (t.get("profit_ratio") or 0) * 100
        color = VERDE if p > 0 else ROJO
        print(f"      {t['pair']:<11} entrada {t['open_rate']:>11,.4f}  "
              f"stop {(t.get('stop_loss_abs') or 0):>11,.4f}  "
              f"stake {t['stake_amount']:>7.2f}  {color}{p:>+6.2f} %{FIN}")

    if detalle:
        pares = config.get("whitelist") or []
        print(f"      {GRIS}senales de entrada en las ultimas {VELAS_SENALES} velas:{FIN}")
        total_global = 0
        for par in pares:
            try:
                n, ultima = senales_recientes(cliente, par)
            except ErrorAPI:
                continue
            total_global += n
            if n:
                marca = f" (ultima: {str(ultima)[:16]})" if ultima else ""
                print(f"        {par:<11} {n:>3}{GRIS}{marca}{FIN}")
        if total_global == 0:
            print(f"        {AMARILLO}ninguna en ningun par{FIN} — la estrategia esta "
                  f"esperando su condicion")
        else:
            print(f"        {GRIS}total: {total_global} senales{FIN}")

    return {"equity": equity, "abiertas": len(abiertas), "cerradas": cerradas, "pnl": pnl}


def main() -> int:
    p = argparse.ArgumentParser(description="Estado de los bots")
    p.add_argument("--bot", choices=list(BOTS), default=None,
                   help="ver solo uno, con el desglose de senales por par")
    p.add_argument("--senales", action="store_true",
                   help="incluir el desglose de senales para todos")
    args = p.parse_args()

    seleccion = {args.bot: BOTS[args.bot]} if args.bot else BOTS
    detalle = bool(args.bot) or args.senales

    print("=" * 78)
    print("ESTADO DE LOS BOTS")
    print("=" * 78)
    print(f"  {GRIS}{'':<2}{'bot':<11}{'estrategia':<18}{'equity':>10}"
          f"{'P&L':>10}{'cerradas':>11}{FIN}")

    resultados = [r for r in
                  (revisar(n, pu, detalle) for n, pu in seleccion.items()) if r]

    if not resultados:
        print("\n  Ningun bot responde.\n  docker compose ps\n  docker compose up -d",
              file=sys.stderr)
        return 1

    if len(resultados) > 1:
        print("-" * 78)
        equity = sum(r["equity"] for r in resultados)
        inicial = 1000.0 * len(resultados)
        print(f"  equity simulada total: {equity:,.2f} de {inicial:,.0f} USDT "
              f"({(equity - inicial) / inicial * 100:+.2f} %)")
        print(f"  posiciones abiertas  : {sum(r['abiertas'] for r in resultados)}")
        print(f"  operaciones cerradas : {sum(r['cerradas'] for r in resultados)}")

    print()
    print(f"  {GRIS}Riesgo por operacion {RIESGO_POR_OPERACION:.1%} · "
          f"maximo {MAX_POSICIONES_SIMULTANEAS} posiciones por bot · "
          f"limite diario {PERDIDA_DIARIA_MAXIMA:.0%} · "
          f"kill switch {DRAWDOWN_TOTAL_MAXIMO:.0%}{FIN}")
    print(f"  {GRIS}Cada bot tiene su propia cartera simulada de 1.000 USDT: son "
          f"comparables entre si.{FIN}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
