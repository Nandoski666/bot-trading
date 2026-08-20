#!/usr/bin/env python3
"""
T9 — Kill switch. Cierra todas las posiciones a mercado y detiene el bot.

Cuando se usa
-------------
  * el drawdown total supera el 10 % (lo dispara `watchdog.py` solo)
  * el exchange se comporta de forma anomala (ordenes rechazadas, precios raros)
  * hay que apagar por mantenimiento con posiciones abiertas
  * algo no cuadra y no sabes que es — esa es razon suficiente

Orden de las operaciones
------------------------
1. **Pausar** — el bot deja de abrir posiciones pero sigue funcionando.
2. **Cerrar** todas las posiciones a mercado.
3. **Verificar** que no queda ninguna abierta.
4. **Detener** el bot del todo.

El orden no es cosmetico, y se aprendio probandolo contra un bot real:

  * Cerrar antes de pausar deja una ventana en la que el bot puede volver a
    entrar, y el kill switch terminaria con la cuenta expuesta despues de
    haberla "cerrado".
  * Detener (`/stop`) antes de cerrar **no funciona**: Freqtrade rechaza
    `forceexit` con el trader detenido ("trader is not running"). Y aunque lo
    aceptara, un bot detenido deja de gestionar las posiciones abiertas — nadie
    mueve el trailing ni ejecuta el stop.

`/pause` es el estado intermedio que hace falta: no entra, pero sigue vivo.

El paso 3 importa aparte: un cierre parcial reportado como exito es peor que un
fallo, porque nadie vuelve a mirar.

Uso:
    python tools/kill_switch.py                 # muestra el estado y pide confirmacion
    python tools/kill_switch.py --confirm       # ejecuta sin preguntar
    python tools/kill_switch.py --solo-detener  # deja de abrir, no cierra nada
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone

from api_freqtrade import ClienteFreqtrade, ErrorAPI


def mostrar_estado(cliente: ClienteFreqtrade) -> list[dict]:
    """Imprime la situacion actual y devuelve las posiciones abiertas."""
    abiertas = cliente.posiciones_abiertas()

    try:
        beneficio = cliente.beneficio()
        print(f"  beneficio cerrado : {beneficio.get('profit_closed_coin', 0):.4f} "
              f"{beneficio.get('best_pair', '')[:0]}"
              f"({beneficio.get('profit_closed_percent', 0):+.2f} %)")
        print(f"  operaciones totales: {beneficio.get('trade_count', 0)}")
    except ErrorAPI:
        pass

    if not abiertas:
        print("  posiciones abiertas: ninguna")
        return []

    print(f"  posiciones abiertas: {len(abiertas)}")
    total = 0.0
    for t in abiertas:
        pnl = t.get("profit_abs") or 0.0
        total += pnl
        print(f"    #{t['trade_id']:<4} {t['pair']:<10} "
              f"entrada {t['open_rate']:>12,.4f}  "
              f"actual {(t.get('current_rate') or 0):>12,.4f}  "
              f"P&L {pnl:>+9.2f} ({(t.get('profit_ratio') or 0) * 100:+.2f} %)")
    print(f"    {'TOTAL':<16}{'':<24}{'':<21}    {total:>+9.2f}")
    return abiertas


def main() -> int:
    p = argparse.ArgumentParser(description="Kill switch — cierra todo y detiene el bot")
    p.add_argument("--url", default="http://127.0.0.1:8080")
    p.add_argument("--confirm", action="store_true",
                   help="ejecutar sin pedir confirmacion (para automatizacion)")
    p.add_argument("--solo-detener", action="store_true",
                   help="pausar (dejar de abrir) sin cerrar las posiciones abiertas")
    p.add_argument("--motivo", default="disparo manual",
                   help="queda registrado en la salida y en el log")
    args = p.parse_args()

    cliente = ClienteFreqtrade(args.url)
    ahora = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print("=" * 70)
    print("KILL SWITCH")
    print("=" * 70)
    print(f"  momento: {ahora}")
    print(f"  motivo : {args.motivo}")
    print()

    try:
        cliente.ping()
    except ErrorAPI as exc:
        print(f"ERROR: {exc}\n", file=sys.stderr)
        print("El bot no responde. Si hay posiciones abiertas, ciérralas A MANO\n"
              "desde la interfaz de Binance. No esperes a que vuelva.\n"
              "Ver docs/RUNBOOK.md, seccion «el bot esta muerto con una posicion abierta».",
              file=sys.stderr)
        return 1

    print("Estado actual:")
    abiertas = mostrar_estado(cliente)
    print()

    if not args.confirm:
        accion = ("PAUSAR el bot (no abrira mas posiciones)" if args.solo_detener
                  else f"PAUSAR el bot y CERRAR {len(abiertas)} posiciones a mercado")
        respuesta = input(f"Se va a {accion}. Escribe 'si' para continuar: ")
        if respuesta.strip().lower() not in ("si", "sí", "s", "yes", "y"):
            print("Cancelado. No se ha tocado nada.")
            return 2
        print()

    # --- Paso 1: pausar -----------------------------------------------------
    # Pausar y no detener: en PAUSED el bot no entra, pero sigue gestionando las
    # posiciones abiertas y la API acepta forceexit. Detenerlo aqui romperia el
    # paso 2.
    print("[1/4] Pausando el bot (no abrira posiciones nuevas)…")
    try:
        r = cliente.pausar()
        print(f"      {r.get('status', 'pausado')}")
    except ErrorAPI as exc:
        print(f"      ERROR al pausar: {exc}", file=sys.stderr)
        print("      Se continua con el cierre: las posiciones son mas urgentes.",
              file=sys.stderr)

    if args.solo_detener:
        print("\nModo --solo-detener: el bot queda PAUSADO. Las posiciones abiertas\n"
              "siguen gestionandose (trailing y stop activos), pero no se abriran\n"
              "nuevas.\n\nPara cerrarlas:  python tools/kill_switch.py --confirm\n"
              "Para reanudar:   /start desde Telegram")
        return 0

    # --- Paso 2: cerrar todo ------------------------------------------------
    if abiertas:
        print(f"[2/4] Cerrando {len(abiertas)} posiciones a mercado…")
        try:
            r = cliente.cerrar_todo()
            print(f"      {r.get('result', r)}")
        except ErrorAPI as exc:
            print(f"      ERROR al cerrar: {exc}", file=sys.stderr)
            print("      CIERRA LAS POSICIONES A MANO EN BINANCE AHORA.", file=sys.stderr)
            return 1
    else:
        print("[2/4] No hay posiciones que cerrar.")

    # --- Paso 3: verificar --------------------------------------------------
    # Las ordenes a mercado tardan un instante en confirmarse. Se comprueba de
    # verdad en vez de asumir que salio bien.
    print("[3/4] Verificando…")
    for intento in range(1, 7):
        time.sleep(2)
        try:
            quedan = cliente.posiciones_abiertas()
        except ErrorAPI as exc:
            print(f"      no se pudo verificar: {exc}", file=sys.stderr)
            return 1
        if not quedan:
            print("      confirmado: 0 posiciones abiertas.")

            # --- Paso 4: ahora si, detener del todo -------------------------
            # Ya no queda nada que gestionar, asi que STOPPED es seguro.
            print("[4/4] Deteniendo el bot…")
            try:
                r = cliente.detener()
                print(f"      {r.get('status', 'detenido')}")
            except ErrorAPI as exc:
                print(f"      aviso: no se pudo detener ({exc}). El bot queda "
                      "pausado, que ya impide abrir posiciones.", file=sys.stderr)

            print()
            print("=" * 70)
            print("KILL SWITCH COMPLETADO — el bot esta detenido y sin exposicion.")
            print("=" * 70)
            print("\nAntes de volver a arrancar:")
            print("  1. Averigua QUE paso. Anotalo en docs/JOURNAL.md.")
            print("  2. Revisa user_data/logs/freqtrade.log alrededor del incidente.")
            print("  3. Si el kill switch salto por drawdown, no reinicies sin haber")
            print("     entendido por que. Reiniciar sin diagnostico repite la perdida.")
            return 0
        print(f"      intento {intento}/6: quedan {len(quedan)} posiciones…")

    print("\nAVISO: siguen abiertas tras 12 s. Puede ser lentitud del exchange o un\n"
          "fallo real. VERIFICA A MANO EN BINANCE antes de dar esto por cerrado.",
          file=sys.stderr)
    mostrar_estado(cliente)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
