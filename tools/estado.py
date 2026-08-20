#!/usr/bin/env python3
"""
Que esta viendo el bot ahora mismo.

Durante un dry-run largo la pregunta que aparece cada dos dias es la misma:
"¿esto esta funcionando o esta colgado?". Con una estrategia que hace ~68
operaciones al año entre 3 pares, semanas enteras sin actividad son lo normal —
y eso hace imposible distinguir "esperando correctamente" de "roto".

Este script contesta la pregunta mirando el dataframe que el propio bot acaba
de analizar: para cada par, el valor real de cada indicador y cual de las cuatro
condiciones de entrada se cumple y cual no. Si el bot esta vivo, aqui se ve.

Uso:
    python tools/estado.py
    python tools/estado.py --url http://127.0.0.1:8080
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
    ATR_MULTIPLICADOR_STOP,
    MAX_POSICIONES_SIMULTANEAS,
    RIESGO_POR_OPERACION,
)

VERDE, ROJO, GRIS, FIN = "\033[32m", "\033[31m", "\033[90m", "\033[0m"
RSI_MINIMO, RSI_MAXIMO = 40, 70


def marca(ok: bool) -> str:
    return f"{VERDE}✓{FIN}" if ok else f"{ROJO}✗{FIN}"


def velas_analizadas(cliente: ClienteFreqtrade, par: str) -> dict:
    """Ultimas velas tal y como las dejo `populate_indicators` del bot.

    Importante: esto no recalcula nada por su cuenta. Lee lo que el bot tiene
    en memoria, asi que si aqui aparece un indicador es que el bot lo calculo.
    """
    return cliente._peticion("GET", "pair_candles",
                             params={"pair": par, "timeframe": "1h", "limit": 3})


def analizar(datos: dict) -> dict | None:
    columnas = datos.get("columns", [])
    filas = datos.get("data", [])
    if not filas:
        return None

    idx = {c: i for i, c in enumerate(columnas)}
    ultima = filas[-1]
    previa = filas[-2] if len(filas) > 1 else ultima

    def v(fila, col):
        i = idx.get(col)
        return fila[i] if i is not None else None

    cierre = v(ultima, "close")
    rapida, lenta = v(ultima, "ema_rapida"), v(ultima, "ema_lenta")
    rapida_prev, lenta_prev = v(previa, "ema_rapida"), v(previa, "ema_lenta")
    regimen, rsi = v(ultima, "ema_regimen"), v(ultima, "rsi")
    volumen, vol_sma = v(ultima, "volume"), v(ultima, "volumen_sma")
    atr = v(ultima, "atr")

    fecha = v(ultima, "date") or v(ultima, "__date_ts")

    return {
        "fecha": fecha,
        "cierre": cierre,
        "atr": atr,
        # La condicion 1 es un EVENTO: la rapida tiene que haber cruzado a la
        # lenta en esta vela concreta, no simplemente estar por encima.
        "cruce": (rapida is not None and lenta is not None
                  and rapida > lenta and rapida_prev <= lenta_prev),
        "rapida_encima": rapida is not None and lenta is not None and rapida > lenta,
        "distancia_emas": ((rapida - lenta) / lenta * 100)
                          if (rapida and lenta) else None,
        "regimen": cierre is not None and regimen is not None and cierre > regimen,
        "distancia_regimen": ((cierre - regimen) / regimen * 100)
                             if (cierre and regimen) else None,
        "rsi": rsi,
        "rsi_ok": rsi is not None and RSI_MINIMO < rsi < RSI_MAXIMO,
        "volumen_ok": volumen is not None and vol_sma is not None and volumen > vol_sma,
        "volumen_ratio": (volumen / vol_sma) if (volumen and vol_sma) else None,
        "senal": bool(v(ultima, "enter_long")),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Que esta viendo el bot ahora mismo")
    p.add_argument("--url", default="http://127.0.0.1:8080")
    args = p.parse_args()

    cliente = ClienteFreqtrade(args.url)

    try:
        salud = cliente.salud()
        config = cliente.estado_bot()
        balance = cliente.balance()
        abiertas = cliente.posiciones_abiertas()
    except ErrorAPI as exc:
        print(f"El bot no responde: {exc}", file=sys.stderr)
        print("\n  docker compose ps\n  docker compose logs freqtrade --tail 50",
              file=sys.stderr)
        return 1

    ultimo = datetime.fromisoformat(str(salud["last_process"]).replace("Z", "+00:00"))
    silencio = (datetime.now(timezone.utc) - ultimo).total_seconds()

    print("=" * 74)
    print("QUE ESTA VIENDO EL BOT")
    print("=" * 74)
    estado_txt = config.get("state", "?")
    modo = "DRY-RUN (dinero simulado)" if config.get("dry_run") else "LIVE (DINERO REAL)"
    print(f"  estado      : {estado_txt} · {modo}")
    print(f"  ultimo ciclo: hace {silencio:.0f} s"
          f"{'  ← el bot esta procesando' if silencio < 120 else f'  {ROJO}← lleva mucho parado{FIN}'}")
    print(f"  equity      : {balance.get('total', 0):,.2f} {balance.get('stake', 'USDT')}")
    print(f"  posiciones  : {len(abiertas)} de {MAX_POSICIONES_SIMULTANEAS}")

    if abiertas:
        print()
        for t in abiertas:
            print(f"    {t['pair']:<10} entrada {t['open_rate']:>12,.2f}  "
                  f"stop {(t.get('stop_loss_abs') or 0):>12,.2f}  "
                  f"P&L {(t.get('profit_ratio') or 0) * 100:>+6.2f} %")

    pares = config.get("exchange", {}) and config.get("whitelist") or \
        ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

    print()
    print("-" * 74)
    print("CONDICIONES DE ENTRADA — sobre la ultima vela cerrada de 1 h")
    print("-" * 74)

    for par in pares:
        try:
            a = analizar(velas_analizadas(cliente, par))
        except ErrorAPI as exc:
            print(f"\n  {par}: no se pudo leer ({exc})")
            continue
        if a is None:
            print(f"\n  {par}: el bot todavia no ha analizado este par")
            continue

        cumplidas = sum([a["cruce"], a["regimen"], a["rsi_ok"], a["volumen_ok"]])
        print(f"\n  {par}   precio {a['cierre']:,.2f}   "
              f"{GRIS}({cumplidas}/4 condiciones){FIN}")

        # 1 — el cruce es un evento, no un estado
        if a["cruce"]:
            detalle = "cruce alcista EN esta vela"
        elif a["rapida_encima"]:
            detalle = (f"EMA20 ya estaba encima (+{a['distancia_emas']:.2f} %) — "
                       "el cruce ya paso, no cuenta")
        else:
            detalle = f"EMA20 por debajo de EMA50 ({a['distancia_emas']:+.2f} %)"
        print(f"    {marca(a['cruce'])} 1. cruce EMA20/EMA50    {GRIS}{detalle}{FIN}")

        # 2 — regimen
        detalle = (f"precio {a['distancia_regimen']:+.2f} % respecto a la EMA200"
                   if a["distancia_regimen"] is not None else "sin datos")
        print(f"    {marca(a['regimen'])} 2. precio > EMA200      {GRIS}{detalle}{FIN}")

        # 3 — RSI
        if a["rsi"] is None:
            detalle = "sin datos"
        elif a["rsi"] <= RSI_MINIMO:
            detalle = f"RSI {a['rsi']:.1f} — debil, por debajo de {RSI_MINIMO}"
        elif a["rsi"] >= RSI_MAXIMO:
            detalle = f"RSI {a['rsi']:.1f} — sobrecompra, por encima de {RSI_MAXIMO}"
        else:
            detalle = f"RSI {a['rsi']:.1f} — dentro de la banda {RSI_MINIMO}-{RSI_MAXIMO}"
        print(f"    {marca(a['rsi_ok'])} 3. RSI entre 40 y 70    {GRIS}{detalle}{FIN}")

        # 4 — volumen
        detalle = (f"volumen {a['volumen_ratio']:.2f}x su media de 20"
                   if a["volumen_ratio"] is not None else "sin datos")
        print(f"    {marca(a['volumen_ok'])} 4. volumen > media 20   {GRIS}{detalle}{FIN}")

        if a["senal"]:
            print(f"    {VERDE}>> SENAL DE ENTRADA ACTIVA{FIN}")
        elif a["atr"]:
            # Que posicion tomaria si entrara ahora. Hace tangible el
            # dimensionamiento por riesgo, que es lo que mas cuesta creerse.
            equity = balance.get("total", 0)
            distancia = ATR_MULTIPLICADOR_STOP * a["atr"]
            stake = equity * RIESGO_POR_OPERACION / (distancia / a["cierre"])
            stake = min(stake, equity / MAX_POSICIONES_SIMULTANEAS)
            print(f"    {GRIS}si entrara ahora: stop en {a['cierre'] - distancia:,.2f} "
                  f"({distancia / a['cierre'] * 100:.2f} % abajo), "
                  f"posicion de {stake:,.2f} USDT{FIN}")

    print()
    print("-" * 74)
    print("""
  La condicion 1 es la que casi siempre falta, y es intencionado: el cruce de
  EMAs es un EVENTO de una sola vela. Estar "por encima" no vale — si valiera,
  el bot intentaria comprar cada hora durante toda una tendencia.

  Por eso pasan semanas sin operaciones. Es la estrategia funcionando, no
  estando rota. Lo que si seria raro: que el ultimo ciclo lleve horas sin
  actualizarse, o que la condicion 2 nunca se cumpla en ningun par.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
