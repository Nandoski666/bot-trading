#!/usr/bin/env python3
"""
Copia la base de datos del bot desde su volumen Docker al host.

Por que hace falta
------------------
El SQLite del bot vive en un volumen nombrado y no en `./user_data`, porque en
un bind mount de macOS los descriptores de archivo se invalidan cuando el equipo
se suspende y el bot empieza a dar "disk I/O error" hasta que se reinicia (ver
el comentario en docker-compose.yml).

El precio de esa robustez es que las herramientas del host —`report.py`,
`entrada_journal.py`— ya no ven el archivo directamente. Este script lo saca.

Se llama solo desde esas herramientas, asi que normalmente no hay que
ejecutarlo a mano.

Uso:
    python tools/exportar_db.py
    python tools/exportar_db.py --destino /tmp/copia.sqlite
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DESTINO_POR_DEFECTO = RAIZ / "user_data" / "tradesv3.dryrun.sqlite"
RUTA_EN_CONTENEDOR = "/freqtrade/db/tradesv3.dryrun.sqlite"
SERVICIO = "freqtrade"


def docker_disponible() -> bool:
    try:
        r = subprocess.run(["docker", "compose", "ps", "-q", SERVICIO],
                           cwd=RAIZ, capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and bool(r.stdout.strip())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def exportar(destino: Path = DESTINO_POR_DEFECTO, silencioso: bool = False) -> Path | None:
    """Saca el SQLite del contenedor. Devuelve la ruta, o None si no se pudo.

    Se copia el archivo entero en vez de consultarlo dentro del contenedor
    porque asi las herramientas del host trabajan contra un fichero normal, sin
    depender de que Docker este corriendo cada vez que se lee.
    """
    if not docker_disponible():
        if not silencioso:
            print("(el contenedor del bot no esta corriendo: se usa la copia "
                  "que haya en el host)", file=sys.stderr)
        return destino if destino.exists() else None

    destino.parent.mkdir(parents=True, exist_ok=True)
    temporal = destino.with_suffix(destino.suffix + ".tmp")

    r = subprocess.run(
        ["docker", "compose", "cp", f"{SERVICIO}:{RUTA_EN_CONTENEDOR}", str(temporal)],
        cwd=RAIZ, capture_output=True, text=True)

    if r.returncode != 0:
        temporal.unlink(missing_ok=True)
        if not silencioso:
            detalle = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
            print(f"(no se pudo copiar la base de datos: {detalle[0]})", file=sys.stderr)
        return destino if destino.exists() else None

    # Reemplazo atomico: si algo falla a mitad, la copia anterior sigue intacta
    # en vez de quedar un archivo a medias que las herramientas leerian como
    # bueno.
    shutil.move(str(temporal), str(destino))
    if not silencioso:
        print(f"base de datos exportada a {destino.relative_to(RAIZ)} "
              f"({destino.stat().st_size:,} bytes)")
    return destino


def main() -> int:
    p = argparse.ArgumentParser(description="Exporta el SQLite del bot al host")
    p.add_argument("--destino", type=Path, default=DESTINO_POR_DEFECTO)
    args = p.parse_args()
    return 0 if exportar(args.destino) else 1


if __name__ == "__main__":
    raise SystemExit(main())
