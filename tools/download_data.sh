#!/usr/bin/env bash
#
# T2 — Descarga de OHLCV historico desde Binance.
#
# Descarga velas de 1h para los 3 pares del universo desde 2021-01-01 hasta hoy.
# La descarga es incremental: Freqtrade solo pide las velas que faltan, asi que
# volver a correr el script tras unos dias es barato.
#
# Uso:
#   ./tools/download_data.sh                 # entorno local (.venv)
#   ./tools/download_data.sh --docker        # dentro del contenedor
#
# No necesita claves de API: los datos OHLCV son publicos.

set -euo pipefail

cd "$(dirname "$0")/.."

PARES=("BTC/USDT" "ETH/USDT" "SOL/USDT")
TIMEFRAMES=("1h" "1d")   # 1d se usa para el benchmark buy-and-hold y validacion cruzada
DESDE="20210101"
FORMATO="feather"

# --- Elegir runner: docker o venv local ---------------------------------------
if [[ "${1:-}" == "--docker" ]]; then
    RUNNER=(docker compose run --rm freqtrade)
    CONFIG="/freqtrade/user_data/config.dryrun.json"
    DATADIR="/freqtrade/user_data/data"
else
    if [[ ! -x .venv/bin/freqtrade ]]; then
        echo "ERROR: no existe .venv/bin/freqtrade. Corre 'make setup' primero." >&2
        exit 1
    fi
    RUNNER=(.venv/bin/freqtrade)
    CONFIG="user_data/config.dryrun.json"
    DATADIR="user_data/data"
fi

echo "==> Descargando OHLCV"
echo "    pares      : ${PARES[*]}"
echo "    timeframes : ${TIMEFRAMES[*]}"
echo "    desde      : ${DESDE}"
echo "    destino    : ${DATADIR}"
echo

"${RUNNER[@]}" download-data \
    --config "${CONFIG}" \
    --exchange binance \
    --pairs "${PARES[@]}" \
    --timeframes "${TIMEFRAMES[@]}" \
    --timerange "${DESDE}-" \
    --data-format-ohlcv "${FORMATO}" \
    --datadir "${DATADIR}"

echo
echo "==> Resumen de lo descargado"
"${RUNNER[@]}" list-data \
    --config "${CONFIG}" \
    --datadir "${DATADIR}" \
    --data-format-ohlcv "${FORMATO}" \
    --show-timerange

echo
echo "==> Siguiente paso: auditar la calidad de los datos"
echo "    python tools/validate_data.py"
