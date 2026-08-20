# Atajos del proyecto. Camino local (uv + .venv) para desarrollo y tests;
# Docker para el bot en ejecución continua.

PY      := .venv/bin/python
FT      := .venv/bin/freqtrade
CONFIG  := user_data/config.dryrun.json
STRAT   := BaselineTrend

.PHONY: setup version test lookahead backtest data clean

## Crear el entorno local con Python 3.12 (Freqtrade no soporta 3.13+)
setup:
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) "freqtrade[plot]" pytest pytest-cov

version:
	$(FT) --version

## Tests unitarios — deben estar en verde antes de cualquier commit
test:
	$(PY) -m pytest tests/ -v

## Detección de sesgo de anticipación (lookahead bias)
lookahead:
	$(FT) lookahead-analysis --config $(CONFIG) --strategy $(STRAT) \
		--timerange 20230101-20240101

## Descargar OHLCV y auditar calidad
data:
	./tools/download_data.sh
	$(PY) tools/validate_data.py

## Backtest in-sample con comisiones y slippage
backtest:
	$(PY) tools/run_backtest.py --window in-sample

clean:
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
