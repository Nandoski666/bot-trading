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

# Se detecta el proveedor por la forma de la clave, para no tener que
# preguntarlo: sk-ant-... es Anthropic, gsk_... es Groq.
PROVEEDORES = {
    "anthropic": {
        "variable": "ANTHROPIC_API_KEY",
        "patron": re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,}$"),
        "consola": "https://console.anthropic.com",
        "nota": "de pago, mejor razonamiento",
    },
    "groq": {
        "variable": "GROQ_API_KEY",
        "patron": re.compile(r"^gsk_[A-Za-z0-9]{20,}$"),
        "consola": "https://console.groq.com/keys",
        "nota": "gratis, muy rapido",
    },
}


def detectar(clave: str) -> str | None:
    for nombre, cfg in PROVEEDORES.items():
        if cfg["patron"].match(clave):
            return nombre
    return None


def ocultar(clave: str) -> str:
    return f"{clave[:12]}…{clave[-4:]}" if len(clave) > 20 else "***"


def escribir_env(variable: str, valor: str) -> None:
    """Actualiza ANTHROPIC_API_KEY sin tocar el resto del archivo.

    Reescribir el .env entero perderia los comentarios, que en ese archivo son
    la documentacion de que hace cada variable.
    """
    lineas = ENV.read_text(encoding="utf-8").splitlines()
    patron = re.compile(rf"^\s*{re.escape(variable)}\s*=")
    for i, linea in enumerate(lineas):
        if patron.match(linea):
            lineas[i] = f"{variable}={valor}"
            break
    else:
        lineas.append(f"{variable}={valor}")
    ENV.write_text("\n".join(lineas) + "\n", encoding="utf-8")


def verificar(clave: str, proveedor: str) -> tuple[bool, str]:
    """Comprueba la clave contra la API con la peticion mas barata posible."""
    if proveedor == "anthropic":
        try:
            import anthropic
        except ImportError:
            return False, "falta el paquete anthropic (uv pip install anthropic)"
        try:
            cliente = anthropic.Anthropic(api_key=clave)
            r = cliente.with_options(timeout=45.0).messages.create(
                model="claude-opus-5", max_tokens=16,
                messages=[{"role": "user", "content": "Responde solo: ok"}])
            return True, "".join(b.text for b in r.content if b.type == "text").strip()[:40]
        except anthropic.AuthenticationError:
            return False, "la clave fue rechazada por Anthropic"
        except anthropic.RateLimitError:
            return True, "clave valida (limite de peticiones alcanzado ahora)"
        except Exception as exc:                  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:120]}"

    try:
        from groq import Groq
    except ImportError:
        return False, "falta el paquete groq (uv pip install groq)"
    try:
        cliente = Groq(api_key=clave, timeout=45.0)
        r = cliente.chat.completions.create(
            model="groq/compound", max_tokens=16,
            messages=[{"role": "user", "content": "Responde solo: ok"}])
        return True, (r.choices[0].message.content or "").strip()[:40]
    except Exception as exc:                      # noqa: BLE001
        mensaje = str(exc)
        if "401" in mensaje or "invalid_api_key" in mensaje.lower():
            return False, "la clave fue rechazada por Groq"
        return False, f"{type(exc).__name__}: {mensaje[:120]}"


def main() -> int:
    p = argparse.ArgumentParser(description="Guardar la clave de IA del filtro")
    p.add_argument("--probar", action="store_true",
                   help="verificar las claves que ya estan en .env")
    args = p.parse_args()

    if not ENV.exists():
        print("No existe .env. Crealo primero:  cp .env.example .env", file=sys.stderr)
        return 1

    texto = ENV.read_text(encoding="utf-8")

    if args.probar:
        encontrada = False
        for nombre, cfg in PROVEEDORES.items():
            actual = ""
            for linea in texto.splitlines():
                if linea.strip().startswith(cfg["variable"] + "="):
                    actual = linea.split("=", 1)[1].strip()
            if not actual:
                continue
            encontrada = True
            print(f"{nombre}: {ocultar(actual)} — verificando…")
            ok, detalle = verificar(actual, nombre)
            print(f"  {'VALIDA' if ok else 'NO VALIDA'} — {detalle}")
        if not encontrada:
            print("No hay ninguna clave de IA en .env.")
            print("El filtro funciona igual, sin bloquear nada.")
        return 0

    print("""
Pega la clave y pulsa Enter. Se detecta sola de que proveedor es:

    sk-ant-...   Anthropic  (de pago, mejor razonamiento)
    gsk_...      Groq       (gratis, muy rapido)

No se vera nada mientras escribes — es lo normal, sigue pegando.
""")
    clave = getpass.getpass("  clave: ").strip().strip('"').strip("'")

    if not clave:
        print("\nNo se introdujo nada. No se ha cambiado el .env.", file=sys.stderr)
        return 2

    proveedor = detectar(clave)
    if proveedor is None:
        print("\nNo reconozco esa clave.\n"
              "  Anthropic empieza por 'sk-ant-'\n"
              "  Groq empieza por 'gsk_'\n"
              "No se ha cambiado el .env.", file=sys.stderr)
        return 2

    cfg = PROVEEDORES[proveedor]
    print(f"\n  proveedor: {proveedor} ({cfg['nota']})")
    print(f"  recibida : {ocultar(clave)}")
    print("  verificando contra la API…")

    ok, detalle = verificar(clave, proveedor)
    if not ok:
        print(f"  RECHAZADA — {detalle}", file=sys.stderr)
        print("\n  No se guarda una clave que la API rechaza.", file=sys.stderr)
        return 1

    print(f"  respuesta del modelo: {detalle}")
    escribir_env(cfg["variable"], clave)
    print(f"  guardada en .env como {cfg['variable']}")

    if proveedor == "groq":
        print("""
  Nota: con las dos claves presentes gana Anthropic. Si quieres forzar Groq,
  deja ANTHROPIC_API_KEY vacia en .env.""")

    print("""
El filtro de contexto ya puede consultar el mercado y las noticias.

    python tools/filtro_ia.py               # una evaluacion ahora
    python tools/filtro_ia.py --explicar    # ver el veredicto vigente
    docker compose up -d filtro-ia          # dejarlo corriendo

Recuerda lo que puede y no puede hacer: solo VETA entradas, nunca las provoca,
y se ignora por completo en backtest.
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
