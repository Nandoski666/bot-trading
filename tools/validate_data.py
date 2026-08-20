#!/usr/bin/env python3
"""
T2 — Auditoria de calidad del dataset OHLCV.

Un backtest sobre datos sucios produce metricas que no significan nada. Antes de
medir cualquier estrategia hay que saber exactamente que tan completo y coherente
es el historico.

Este script revisa, por par y timeframe:

  * velas faltantes  — huecos respecto a la rejilla temporal esperada
  * gaps largos      — interrupciones mayores a 2 velas seguidas
  * volumen en cero  — velas sin operaciones (sospechosas de relleno sintetico)
  * duplicados       — timestamps repetidos
  * incoherencias    — high < low, close fuera del rango [low, high], precios <= 0
  * saltos de precio — variaciones intervela extremas (posibles errores del feed)

Salida: user_data/data/DATA_REPORT.md

Umbral de aceptacion (definicion de hecho del ticket T2):
    < 0.5 % de velas faltantes por par. Si se supera, hay que documentar el rango
    afectado y excluirlo de los backtests.

Uso:
    python tools/validate_data.py
    python tools/validate_data.py --datadir user_data/data --timeframe 1h
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# Universo y parametros fijados en el plan (seccion 2).
PARES = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
TIMEFRAME = "1h"
UMBRAL_FALTANTES_PCT = 0.5   # definicion de hecho de T2
GAP_MAXIMO_VELAS = 2         # un gap > 2 velas se reporta explicitamente
SALTO_EXTREMO_PCT = 25.0     # variacion close-a-close que merece revision manual

MINUTOS_POR_TIMEFRAME = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "4h": 240, "1d": 1440,
}


# ---------------------------------------------------------------------------
# Carga
# ---------------------------------------------------------------------------

def ruta_par(datadir: Path, par: str, timeframe: str) -> Path | None:
    """Localiza el archivo de datos de un par. Freqtrade usa 'BTC_USDT-1h.feather'."""
    base = par.replace("/", "_")
    for ext in ("feather", "json", "parquet"):
        candidato = datadir / "binance" / f"{base}-{timeframe}.{ext}"
        if candidato.exists():
            return candidato
        candidato = datadir / f"{base}-{timeframe}.{ext}"
        if candidato.exists():
            return candidato
    return None


def cargar(ruta: Path) -> pd.DataFrame:
    """Lee el OHLCV y normaliza la columna de fecha a UTC."""
    if ruta.suffix == ".feather":
        df = pd.read_feather(ruta)
    elif ruta.suffix == ".parquet":
        df = pd.read_parquet(ruta)
    else:
        df = pd.read_json(ruta)
        df.columns = ["date", "open", "high", "low", "close", "volume"]
        df["date"] = pd.to_datetime(df["date"], unit="ms", utc=True)

    df["date"] = pd.to_datetime(df["date"], utc=True)
    return df.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# Chequeos
# ---------------------------------------------------------------------------

def auditar(df: pd.DataFrame, timeframe: str) -> dict:
    """Devuelve un diccionario con todas las metricas de calidad del par."""
    paso = pd.Timedelta(minutes=MINUTOS_POR_TIMEFRAME[timeframe])
    inicio, fin = df["date"].iloc[0], df["date"].iloc[-1]

    # Rejilla temporal completa que *deberia* existir entre el primer y ultimo dato.
    esperado = pd.date_range(start=inicio, end=fin, freq=paso, tz="UTC")
    presentes = pd.DatetimeIndex(df["date"])
    faltantes = esperado.difference(presentes)

    # Agrupar timestamps faltantes consecutivos en rangos legibles.
    gaps: list[tuple[pd.Timestamp, pd.Timestamp, int]] = []
    if len(faltantes) > 0:
        bloque_ini = bloque_fin = faltantes[0]
        for ts in faltantes[1:]:
            if ts - bloque_fin == paso:
                bloque_fin = ts
            else:
                gaps.append((bloque_ini, bloque_fin,
                             int((bloque_fin - bloque_ini) / paso) + 1))
                bloque_ini = bloque_fin = ts
        gaps.append((bloque_ini, bloque_fin,
                     int((bloque_fin - bloque_ini) / paso) + 1))

    # Coherencia OHLC: si el feed esta corrupto, esto lo delata.
    incoherentes = df[
        (df["high"] < df["low"])
        | (df["close"] > df["high"]) | (df["close"] < df["low"])
        | (df["open"] > df["high"]) | (df["open"] < df["low"])
        | (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
    ]

    # Saltos de precio extremos entre velas cerradas consecutivas.
    variacion = df["close"].pct_change().abs() * 100
    saltos = df.loc[variacion > SALTO_EXTREMO_PCT, ["date", "close"]].copy()
    saltos["variacion_pct"] = variacion[variacion > SALTO_EXTREMO_PCT].round(2)

    return {
        "inicio": inicio,
        "fin": fin,
        "velas": len(df),
        "esperadas": len(esperado),
        "faltantes": len(faltantes),
        "faltantes_pct": round(100 * len(faltantes) / len(esperado), 4) if len(esperado) else 0.0,
        "gaps": gaps,
        "gaps_largos": [g for g in gaps if g[2] > GAP_MAXIMO_VELAS],
        "duplicados": int(df["date"].duplicated().sum()),
        "volumen_cero": int((df["volume"] == 0).sum()),
        "nulos": int(df.isna().sum().sum()),
        "incoherentes": len(incoherentes),
        "saltos": saltos,
        "aprueba": (100 * len(faltantes) / len(esperado) if len(esperado) else 0) < UMBRAL_FALTANTES_PCT
                   and int(df["date"].duplicated().sum()) == 0
                   and len(incoherentes) == 0,
    }


# ---------------------------------------------------------------------------
# Reporte
# ---------------------------------------------------------------------------

def escribir_reporte(resultados: dict[str, dict], destino: Path,
                     timeframe: str, faltantes_sin_datos: list[str]) -> None:
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    L: list[str] = []
    a = L.append

    a("# Reporte de calidad de datos (T2)\n")
    a(f"*Generado: {ahora} — `python tools/validate_data.py`*\n")
    a(f"Timeframe auditado: **{timeframe}** · "
      f"Umbral de aceptacion: **< {UMBRAL_FALTANTES_PCT} %** de velas faltantes por par.\n")

    if faltantes_sin_datos:
        a("> **Faltan descargas:** " + ", ".join(f"`{p}`" for p in faltantes_sin_datos)
          + ". Corre `./tools/download_data.sh`.\n")

    # --- Tabla resumen ---
    a("## Resumen\n")
    a("| Par | Desde | Hasta | Velas | Faltantes | % | Dup. | Vol=0 | OHLC malo | Veredicto |")
    a("|---|---|---|---:|---:|---:|---:|---:|---:|---|")
    for par, r in resultados.items():
        veredicto = "PASA" if r["aprueba"] else "**REVISAR**"
        a(f"| `{par}` | {r['inicio']:%Y-%m-%d} | {r['fin']:%Y-%m-%d} | "
          f"{r['velas']:,} | {r['faltantes']:,} | {r['faltantes_pct']:.3f} | "
          f"{r['duplicados']} | {r['volumen_cero']} | {r['incoherentes']} | {veredicto} |")
    a("")

    todos_pasan = all(r["aprueba"] for r in resultados.values()) and not faltantes_sin_datos
    if todos_pasan:
        a("**Definicion de hecho de T2: CUMPLIDA.** Todos los pares por debajo del "
          f"{UMBRAL_FALTANTES_PCT} % de velas faltantes, sin duplicados ni velas incoherentes.\n")
    else:
        a("**Definicion de hecho de T2: NO CUMPLIDA.** Ver el detalle por par y excluir "
          "de los backtests los rangos afectados.\n")

    # --- Detalle por par ---
    a("## Detalle por par\n")
    for par, r in resultados.items():
        a(f"### `{par}`\n")
        a(f"- Rango: `{r['inicio']:%Y-%m-%d %H:%M}` → `{r['fin']:%Y-%m-%d %H:%M}` UTC")
        a(f"- Velas presentes / esperadas: **{r['velas']:,} / {r['esperadas']:,}**")
        a(f"- Faltantes: **{r['faltantes']:,} ({r['faltantes_pct']:.3f} %)**")
        a(f"- Timestamps duplicados: **{r['duplicados']}**")
        a(f"- Velas con volumen 0: **{r['volumen_cero']}**")
        a(f"- Valores nulos: **{r['nulos']}**")
        a(f"- Velas con OHLC incoherente: **{r['incoherentes']}**")
        a("")

        if r["gaps_largos"]:
            a(f"**Gaps de mas de {GAP_MAXIMO_VELAS} velas** "
              f"({len(r['gaps_largos'])} bloques) — candidatos a excluir del backtest:\n")
            a("| Desde (UTC) | Hasta (UTC) | Velas | Duracion |")
            a("|---|---|---:|---|")
            for ini, fin, n in sorted(r["gaps_largos"], key=lambda g: -g[2])[:25]:
                horas = n * MINUTOS_POR_TIMEFRAME[timeframe] / 60
                a(f"| {ini:%Y-%m-%d %H:%M} | {fin:%Y-%m-%d %H:%M} | {n} | {horas:.0f} h |")
            if len(r["gaps_largos"]) > 25:
                a(f"\n*(+{len(r['gaps_largos']) - 25} bloques mas, omitidos)*")
            a("")
        elif r["gaps"]:
            a(f"Solo gaps cortos (≤ {GAP_MAXIMO_VELAS} velas): {len(r['gaps'])} bloques. "
              "Compatibles con mantenimientos puntuales de Binance.\n")
        else:
            a("Sin gaps. Serie continua.\n")

        if len(r["saltos"]):
            a(f"**Saltos de precio > {SALTO_EXTREMO_PCT} % entre velas consecutivas** "
              f"({len(r['saltos'])}) — revisar si son eventos reales o errores del feed:\n")
            a("| Fecha (UTC) | Close | Variacion |")
            a("|---|---:|---:|")
            for _, fila in r["saltos"].head(15).iterrows():
                a(f"| {fila['date']:%Y-%m-%d %H:%M} | {fila['close']:,.2f} | "
                  f"{fila['variacion_pct']:.2f} % |")
            a("")

    # --- Como se usa esto ---
    a("## Que hacer con este reporte\n")
    a("1. Si un par supera el umbral, **no se arregla rellenando velas**: se documenta "
      "el rango y se excluye del backtest con `--timerange`.")
    a("2. Los gaps largos coinciden normalmente con mantenimientos del exchange o con "
      "el listado tardio de un par (SOL/USDT no existe antes de su listado).")
    a("3. Volumen 0 en muchas velas seguidas es senal de datos rellenados: sobre esas "
      "zonas el backtest asume liquidez que no existio.\n")

    destino.write_text("\n".join(L), encoding="utf-8")


# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Auditoria de calidad del OHLCV descargado")
    p.add_argument("--datadir", default="user_data/data", type=Path)
    p.add_argument("--timeframe", default=TIMEFRAME)
    p.add_argument("--pares", nargs="*", default=PARES)
    p.add_argument("--salida", default=None, type=Path)
    args = p.parse_args()

    destino = args.salida or (args.datadir / "DATA_REPORT.md")

    resultados: dict[str, dict] = {}
    sin_datos: list[str] = []

    for par in args.pares:
        ruta = ruta_par(args.datadir, par, args.timeframe)
        if ruta is None:
            print(f"  {par:<10} SIN DATOS — falta descargar", file=sys.stderr)
            sin_datos.append(par)
            continue
        df = cargar(ruta)
        r = auditar(df, args.timeframe)
        resultados[par] = r
        estado = "PASA" if r["aprueba"] else "REVISAR"
        print(f"  {par:<10} {r['velas']:>7,} velas · "
              f"{r['faltantes']:>4,} faltantes ({r['faltantes_pct']:.3f} %) · {estado}")

    if not resultados:
        print("\nNo hay datos que auditar. Corre ./tools/download_data.sh", file=sys.stderr)
        return 1

    destino.parent.mkdir(parents=True, exist_ok=True)
    escribir_reporte(resultados, destino, args.timeframe, sin_datos)
    print(f"\nReporte escrito en {destino}")

    ok = all(r["aprueba"] for r in resultados.values()) and not sin_datos
    print("Definicion de hecho T2:", "CUMPLIDA" if ok else "NO CUMPLIDA (ver reporte)")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
