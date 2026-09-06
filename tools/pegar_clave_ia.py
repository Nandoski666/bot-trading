#!/usr/bin/env python3
"""
Guarda la clave de la API de Claude en .env sin que pase por ningun sitio.

Por que existe este modo y no "editalo a mano":

  * `getpass` no muestra lo que escribes: la clave no queda en pantalla ni en
    una captura.
  * Al no ser argumento de linea de comandos, no entra en ~/.zsh_history, donde
    se quedaria en claro para siempre.
  * No pasa por el portapapeles compartido ni por ninguna conversacion.

La clave va directa de tu teclado a .env. Nadie mas la ve por el camino.

Uso:
    python tools/pegar_clave_ia.py
    python tools/pegar_clave_ia.py --probar   # comprobar la que ya hay
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
ENV = RAIZ / ".env"

# Forma de una clave de Anthropic: sk-ant- seguido de una cadena larga.
PATRON = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,}$")


def ocultar(clave: str) -> str:
    return f"{clave[:12]}…{clave[-4:]}" if len(clave) > 20 else "***"


def escribir_env(valor: str) -> None:
    """Actualiza ANTHROPIC_API_KEY sin tocar el resto del archivo.

    Reescribir el .env entero perderia los comentarios, que en ese archivo son
    la documentacion de que hace cada variable.
    """
    lineas = ENV.read_text(encoding="utf-8").splitlines()
    patron = re.compile(r"^\s*ANTHROPIC_API_KEY\s*=")
    for i, linea in enumerate(lineas):
        if patron.match(linea):
            lineas[i] = f"ANTHROPIC_API_KEY={valor}"
            break
    else:
        lineas.append(f"ANTHROPIC_API_KEY={valor}")
    ENV.write_text("\n".join(lineas) + "\n", encoding="utf-8")


def verificar(clave: str) -> tuple[bool, str]:
    """Comprueba la clave contra la API con la peticion mas barata posible."""
    try:
        import anthropic
    except ImportError:
        return False, "falta el paquete anthropic (uv pip install anthropic)"

    try:
        cliente = anthropic.Anthropic(api_key=clave)
        respuesta = cliente.with_options(timeout=45.0).messages.create(
            model="claude-opus-5",
            max_tokens=16,
            messages=[{"role": "user", "content": "Responde solo: ok"}],
        )
        texto = "".join(b.text for b in respuesta.content if b.type == "text")
        return True, texto.strip()[:40]
    except anthropic.AuthenticationError:
        return False, "la clave fue rechazada por Anthropic"
    except anthropic.PermissionDeniedError:
        return False, "la clave no tiene permiso para este modelo"
    except anthropic.RateLimitError:
        return True, "clave valida (limite de peticiones alcanzado ahora mismo)"
    except anthropic.APIStatusError as exc:
        return False, f"{exc.status_code}: {str(exc)[:120]}"
    except anthropic.APIConnectionError as exc:
        return False, f"no se pudo conectar: {str(exc)[:120]}"
    except Exception as exc:                      # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


def main() -> int:
    p = argparse.ArgumentParser(description="Guardar la clave de la API de Claude")
    p.add_argument("--probar", action="store_true",
                   help="verificar la clave que ya esta en .env")
    args = p.parse_args()

    if not ENV.exists():
        print("No existe .env. Crealo primero:  cp .env.example .env", file=sys.stderr)
        return 1

    if args.probar:
        actual = ""
        for linea in ENV.read_text(encoding="utf-8").splitlines():
            if linea.strip().startswith("ANTHROPIC_API_KEY="):
                actual = linea.split("=", 1)[1].strip()
        if not actual:
            print("No hay ANTHROPIC_API_KEY en .env.")
            print("El filtro de contexto funciona igual, sin bloquear nada.")
            return 0
        print(f"clave en .env: {ocultar(actual)}")
        print("verificando…")
        ok, detalle = verificar(actual)
        print(f"  {'VALIDA' if ok else 'NO VALIDA'} — {detalle}")
        return 0 if ok else 1

    print("""
Pega la clave de https://console.anthropic.com y pulsa Enter.
No se vera nada mientras escribes — es lo normal, sigue pegando.
""")
    clave = getpass.getpass("  ANTHROPIC_API_KEY: ").strip().strip('"').strip("'")

    if not clave:
        print("\nNo se introdujo nada. No se ha cambiado el .env.", file=sys.stderr)
        return 2

    if not PATRON.match(clave):
        print("\nEso no tiene forma de clave de Anthropic.\n"
              "Deberia empezar por 'sk-ant-' seguido de una cadena larga.\n"
              "No se ha cambiado el .env.", file=sys.stderr)
        return 2

    print(f"\n  recibida: {ocultar(clave)}")
    print("  verificando contra la API…")
    ok, detalle = verificar(clave)
    if not ok:
        print(f"  RECHAZADA — {detalle}", file=sys.stderr)
        print("\n  No se guarda una clave que la API rechaza.", file=sys.stderr)
        return 1

    print(f"  respuesta del modelo: {detalle}")
    escribir_env(clave)
    print("  guardada en .env")
    print("""
El filtro de contexto ya puede consultar el mercado y las noticias.

    docker compose up -d filtro-ia          # arrancarlo
    python tools/filtro_ia.py               # una evaluacion ahora
    python tools/filtro_ia.py --explicar    # ver el veredicto vigente

Recuerda lo que puede y no puede hacer: solo VETA entradas, nunca las provoca,
y se ignora por completo en backtest.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
