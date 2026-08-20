# Bot de trading algorítmico — cripto spot

Sistema de trading automatizado sobre **Binance spot**, construido con
[Freqtrade](https://www.freqtrade.io/). El valor de este repositorio no está en
la estrategia (la baseline es deliberadamente estándar), sino en la **máquina de
validación**: backtest con costos reales, walk-forward, tests de riesgo, dry-run
prolongado y criterios go/no-go que no se negocian.

> **Esto no es asesoría financiera.** El sistema puede perder dinero. La mayoría
> de bots retail pierden por sobreajuste, no por bugs.

---

## Estado del proyecto

| Ticket | Descripción | Estado |
|---|---|---|
| T1 | Bootstrap del proyecto | ✅ |
| T2 | Descarga y validación de datos | ⬜ |
| T3 | Estrategia `BaselineTrend` | ⬜ |
| T4 | Tests unitarios | ⬜ |
| T5 | Backtest reproducible | ⬜ |
| T6 | Walk-forward analysis | ⬜ |
| T7 | Reporte de métricas | ⬜ |
| T8 | Configuración dry-run | ⬜ |
| T9 | Kill switch y límites | ⬜ |
| T10 | Documentación operativa | ⬜ |

---

## Requisitos

- **Docker Desktop** (camino principal, reproducible)
- o **Python 3.11/3.12** + [`uv`](https://docs.astral.sh/uv/) (camino local, para
  desarrollo y tests rápidos)

Freqtrade **no soporta Python 3.14**. Si tu `python3` del sistema es 3.13+, usa
el entorno virtual que crea `make setup` (fija 3.12) o Docker.

---

## Puesta en marcha

### 1. Credenciales

```bash
cp .env.example .env
```

Rellena `.env` con:

- Claves de **testnet** de Binance (`https://testnet.binance.vision`) para todo
  el desarrollo. Las claves reales solo entran en la fase live.
- Token y chat ID de Telegram (crear bot con `@BotFather`).
- Contraseña y `JWT_SECRET_KEY` para FreqUI:
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```

`.env` está en `.gitignore`. Nunca se sube. Nunca se pega en un chat.

### 2. Verificar el entorno

**Con Docker:**

```bash
docker compose run --rm freqtrade --version
```

**Sin Docker (entorno local):**

```bash
make setup
make version
```

---

## Flujo completo

El orden importa: cada fase es un filtro que la anterior tiene que pasar.

```
descargar datos  →  backtest in-sample  →  walk-forward  →  dry-run 4+ semanas  →  live
     T2                    T5                   T6               T8/T9           go/no-go
```

```bash
# T2 — Descargar y auditar OHLCV (BTC, ETH, SOL · 1h · desde 2021-01-01)
./tools/download_data.sh
python tools/validate_data.py          # genera user_data/data/DATA_REPORT.md

# T4 — Tests (deben estar en verde antes de cualquier commit)
make test

# T3 — Verificar que no hay sesgo de anticipación
make lookahead

# T5 — Backtest in-sample con comisiones y slippage
make backtest

# T6 — Walk-forward (mide sobreajuste)
python tools/walk_forward.py

# T7 — Reporte comparable
python tools/report.py --backtest user_data/backtest_results/<archivo>.json

# T8 — Dry-run (papel, precios reales)
docker compose up -d
docker compose logs -f
```

FreqUI queda en <http://localhost:8080>.

---

## Reglas de riesgo (no configurables)

| Regla | Valor |
|---|---|
| Riesgo por operación | 0.5 % del equity |
| Posiciones simultáneas máx. | 3 |
| Pérdida diaria máxima | 3 % → deja de abrir posiciones |
| Drawdown total máximo | 10 % → kill switch |
| Apalancamiento | 0 (solo spot) |

Estas cifras están hard-coded en `user_data/strategies/BaselineTrend.py` y
verificadas en `tests/test_risk_rules.py`. **Ningún hyperopt puede tocarlas.**

---

## Criterios go/no-go antes de dinero real

El bot no pasa a live si falla alguno, medido **fuera de muestra**:

- [ ] ≥ 100 operaciones en el backtest
- [ ] Profit factor > 1.2 después de comisiones y slippage
- [ ] Max drawdown < 20 %
- [ ] Sharpe anualizado > 1.0
- [ ] Degradación in-sample → out-of-sample < 40 %
- [ ] Gana a buy-and-hold de BTC en Calmar ratio
- [ ] ≥ 4 semanas de dry-run con desviación < 15 % vs. backtest del mismo periodo

Si falla uno, se vuelve a la fase de estrategia. **No se ajustan los criterios
para que pase.**

---

## Documentación

- [`docs/STRATEGY.md`](docs/STRATEGY.md) — las reglas en español, sin código
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — qué hacer cuando algo falla
- [`docs/JOURNAL.md`](docs/JOURNAL.md) — bitácora semanal (la llena el humano)

---

## Emergencia

```bash
python tools/kill_switch.py --confirm
```

Cierra todas las posiciones a mercado y detiene el bot. Desde Telegram:
`/stop` detiene la apertura de nuevas posiciones, `/forceexit all` cierra todo.
