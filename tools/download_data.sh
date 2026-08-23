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

# Universo ampliado a 12 pares liquidos (seccion 9 del plan: "ampliar el
# universo a 10-15 pares liquidos"). Mas pares = mas oportunidades para las
# mismas reglas, sin relajar ninguna condicion de entrada.
PARES=("BTC/USDT" "ETH/USDT" "SOL/USDT" "BNB/USDT" "XRP/USDT" "ADA/USDT" "AVAX/USDT" "LINK/USDT" "DOT/USDT" "POL/USDT" "LTC/USDT" "ATOM/USDT")
TIMEFRAMES=("1h" "1d")   # 1d se usa para el benchmark buy-and-hold y validacion cruzada
# Empieza 2 meses antes del inicio del backtest (2021-01-01) a proposito:
# la estrategia necesita 600 velas de calentamiento para que la EMA(200)
# converja. Sin ese margen, las primeras semanas del backtest operarian con
# un filtro de regimen a medio calcular, o Freqtrade recortaria la ventana.
DESDE="20201101"
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

descargar() {
    "${RUNNER[@]}" download-data \
        --config "${CONFIG}" \
        --exchange binance \
        --pairs "${PARES[@]}" \
        --timeframes "${TIMEFRAMES[@]}" \
        --timerange "${DESDE}-" \
        --data-format-ohlcv "${FORMATO}" \
        --datadir "${DATADIR}" \
        "$@"
}

# Dos pasadas, porque Freqtrade no hace las dos cosas a la vez:
#
#   --prepend  rellena hacia ATRAS hasta ${DESDE}. Solo hace falta la primera
#              vez, o cuando se adelanta la fecha de inicio. Sin esto, un
#              dataset ya existente que empiece mas tarde se queda como esta y
#              el backtest arranca con menos calentamiento del que cree tener.
#   (normal)   anade hacia ADELANTE las velas nuevas desde la ultima descarga.
#
# Las dos son idempotentes: repetirlas no vuelve a bajar lo que ya esta.
echo "--> pasada 1/2: rellenando hacia atras (--prepend)"
descargar --prepend

echo
echo "--> pasada 2/2: anadiendo velas nuevas"
descargar

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
