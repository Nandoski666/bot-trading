# Bot de trading algorítmico — cripto spot

**Cinco estrategias en paralelo**, cada una en su bot, todas con las mismas
reglas de riesgo y todas medidas contra los mismos criterios.

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
| T2 | Descarga y validación de datos | ✅ 50.832 velas/par, 0.039 % faltantes |
| T3 | Estrategia `BaselineTrend` | ✅ `lookahead-analysis`: sin sesgo |
| T4 | Tests unitarios | ✅ 92 tests, 94 % de cobertura |
| T5 | Backtest reproducible | ✅ 237 ops in-sample, con costos |
| T6 | Walk-forward analysis | ✅ 18 ventanas, 118 ops fuera de muestra |
| T7 | Reporte de métricas | ✅ |
| T8 | Configuración dry-run | ✅ configs listas · falta correr 24 h |
| T9 | Kill switch y límites | ✅ |
| T10 | Documentación operativa | ✅ |

**El bot NO está listo para dinero real, y no por falta de tickets.** La
estrategia baseline no pasa los criterios go/no-go: pierde dinero en el
backtest in-sample. Eso era lo esperado — el proyecto es la máquina de probar
ideas, y esta es la primera idea. Ver
[el reporte de métricas](#resultados-medidos).

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

- Contraseña y `JWT_SECRET_KEY` para FreqUI:
  ```bash
  python -c "import secrets; print(secrets.token_hex(32))"
  ```
- Claves de **testnet** de Binance (`https://testnet.binance.vision`) para todo
  el desarrollo. Las claves reales solo entran en la fase live. **El dry-run no
  las necesita**: los datos de precios son públicos.

`.env` está en `.gitignore`. Nunca se sube. Nunca se pega en un chat.

### 2. Telegram

Crea el bot con `@BotFather` (`/newbot`) y mete el token sin que quede en el
historial del shell:

```bash
python tools/setup_telegram.py --pegar-token
```

Luego abre el chat con tu bot, pulsa **Iniciar**, y deja que el asistente
termine:

```bash
python tools/setup_telegram.py
```

Averigua el chat ID solo, manda un mensaje de prueba y activa Telegram.
Para comprobar sin cambiar nada: `python tools/setup_telegram.py --probar`.

Hasta que lo configures, `FREQTRADE__TELEGRAM__ENABLED=false` en `.env` permite
que el bot arranque sin notificaciones.

### 3. Verificar el entorno

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

## Resultados medidos

Backtest in-sample (2021-01-01 → 2024-06-30), **con 0.15 % de coste por lado**:

| Métrica | BaselineTrend | Buy & hold BTC | Criterio go/no-go |
|---|---:|---:|---|
| Operaciones | 237 | — | ≥ 100 ✅ |
| Profit factor | 0.60 | — | > 1.2 ❌ |
| Beneficio total | −16.74 % | +87.14 % | — |
| Max drawdown | 17.45 % | 76.63 % | < 20 % ✅ |
| Sharpe | −1.79 | 0.61 | > 1.0 ❌ |
| Calmar | −0.30 | 0.26 | ganar a B&H ❌ |

### Walk-forward (T6) — 18 ventanas de 12 meses train + 3 de test

Encadenando los 18 tramos de prueba se obtienen **118 operaciones enteramente
fuera de muestra**, entre 2022-01 y 2026-07:

| | Operaciones | Profit factor |
|---|---:|---:|
| Entrenamiento (dentro de muestra) | 520 | 0.42 |
| Prueba (fuera de muestra) | 118 | 0.36 |

Degradación agregada: **14.0 %**, por debajo del umbral del 40 %. **Y aun así no
pasa.** Los dos profit factors están por debajo de 1.0: la estrategia pierde
dinero dentro *y* fuera de muestra. La degradación es baja porque no se puede
caer mucho desde el suelo, no porque el sistema generalice.

El problema no es sobreajuste. Es más básico: la estrategia no funciona ni
siquiera donde se ajustaron sus parámetros.

El reporte completo está en
`user_data/backtest_results/walk_forward/WALK_FORWARD_REPORT.md`, con la curva
de equity concatenada de los tramos fuera de muestra.

**Veredicto: no pasa a live.** Los criterios no se ajustan para que pase.

Regenerar estos números:

```bash
python tools/report.py --backtest user_data/backtest_results/<archivo>.zip
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

# T6 — Walk-forward (mide sobreajuste). Tarda ~40 min.
python tools/walk_forward.py --solo-listar     # ver las ventanas primero
python tools/walk_forward.py --epochs 40
python tools/walk_forward.py --solo-reporte    # regenerar el reporte sin re-optimizar

# T7 — Reporte comparable
python tools/report.py --backtest user_data/backtest_results/<archivo>.json

# T8 — Dry-run (papel, precios reales)
docker compose up -d
docker compose logs -f

# T9 — Vigilante: heartbeat, límite diario, drawdown, resumen a Telegram
python tools/watchdog.py --intervalo 300
```

El vigilante corre **al lado** del bot, no dentro. Es deliberado: la mitad de
lo que hay que vigilar son cosas que ocurren cuando el bot deja de funcionar.

### Velas de 5 minutos

Los cinco bots corren las variantes `*Rapida`: mismo código de señal y mismo
riesgo, pero sobre velas de 5 minutos. Las operaciones duran ~30 minutos en vez
de horas, así que la maquinaria se verifica en 48 h en vez de en un mes.

**No son una mejora.** Medido sobre 2024-02 → 2026-02, cuatro de las cinco
pierden casi toda la cuenta simulada. El detalle y el porqué están en
[`RAPIDAS.md`](user_data/backtest_results/comparativa/RAPIDAS.md).

### Aprendizaje automático (FreqAI)

`AprendizModelo` entrena un modelo LightGBM sobre ventanas móviles de 30 días,
reentrenando cada 7. Predice el retorno de la próxima hora y solo entra cuando
la expectativa supera los costes.

```bash
python -m freqtrade backtesting --strategy AprendizModelo     --freqaimodel LightGBMRegressor --config user_data/config.dryrun.json     --datadir user_data/data --timerange 20260601-20260815 --fee 0.0015
```

**Qué aprende y qué no.** No aprende de sus propias operaciones — eso sería
imposible de validar y sería sobreajuste puro. Aprende de la estructura del
mercado sobre una ventana que se desplaza, descartando lo viejo. Por eso es
backtesteable: en cada punto del pasado el modelo solo usó datos anteriores.

`do_predict == 1` es la condición que impide operar cuando el mercado entra en
un régimen que el modelo no vio: extrapolar fuera del dominio de entrenamiento
no es predecir, es inventar con decimales.

### Filtro de contexto con IA

```bash
python tools/pegar_clave_ia.py     # meter la clave (Anthropic o Groq)
python tools/filtro_ia.py          # una evaluación ahora
```

Funciona con **dos proveedores**, y elige solo según la clave que haya en `.env`:

| | Coste | Notas |
|---|---|---|
| **Anthropic** (`sk-ant-…`) | de pago | mejor razonamiento |
| **Groq** (`gsk_…`) | **gratis** | muy rápido, modelos `compound` |

Los dos soportan lo que el filtro necesita: búsqueda web y salida estructurada
con esquema estricto. Con las dos claves presentes gana Anthropic.

Cada hora, un contenedor pregunta a Claude si hay alguna razón de **contexto**
para dejar de abrir posiciones — **buscando noticias en la web** por su cuenta —
y escribe su veredicto en `user_data/decision_ia.json`. Las estrategias lo consultan antes de confirmar
una entrada.

Tres reglas que lo mantienen dentro de lo auditable:

1. **Solo puede restar operaciones**, nunca provocarlas. No genera señales.
2. **Se ignora por completo en backtest.** Si afectara al backtest, ningún
   resultado histórico volvería a ser reproducible.
3. **Falla abierto.** Sin `ANTHROPIC_API_KEY`, con la API caída o con el
   veredicto caducado, se opera.

Hay tests que fallan si alguna de las tres se rompe.

### Telegram

El **vigilante** es la única voz en Telegram — los cinco bots tienen su
integración desactivada a propósito (Telegram solo admite un cliente por token,
y compartiéndolo se pelean por el canal).

| Comando | Qué hace |
|---|---|
| `/estado` | qué tiene abierto cada bot |
| `/ops` | últimas operaciones y si se ganaron |
| `/pausar` · `/reanudar` | los cinco a la vez |

Los cierres se notifican solos, con el nombre de la estrategia.

### ¿Está funcionando?

```bash
python tools/estado.py               # los cinco bots de un vistazo
python tools/estado.py --operaciones # + las operaciones cerradas, ganadas y perdidas
python tools/estado.py --bot orochi  # uno solo, con señales por par
```

Muestra si cada bot está procesando, qué posiciones tiene y cuántas señales de
entrada ha producido su estrategia últimamente. Es la forma de distinguir
«esperando correctamente» de «colgado».

### Rutina semanal del dry-run

```bash
python tools/entrada_journal.py --anadir    # rellena las tablas con datos reales
```

Genera la entrada de `docs/JOURNAL.md` leyendo la base de datos del bot y deja
en blanco las tres preguntas que tienes que contestar tú — que son las que
valen. Las cifras nunca se escriben a mano: una bitácora con números inventados
o mal copiados es peor que no tener bitácora, porque dentro de un mes decidirías
comparando contra algo que no ocurrió.

FreqUI queda en <http://localhost:8080>.

> **Si lo corres en un portátil:** macOS se suspende aunque esté enchufado, y
> con él la máquina virtual de Docker. El bot deja de procesar velas durante
> esos minutos. Para el dry-run, `caffeinate -dimsu &` evita la suspensión por
> inactividad (no la de cerrar la tapa). Para dinero real, un VPS — cada
> suspensión es una ventana en la que nadie gestiona una posición abierta.

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

## Llevarlo a una máquina 24/7

```bash
./tools/preparar_despliegue.sh
```

Verifica Docker, RAM, `.env`, datos y —lo que más importa— el **arranque
automático**. Ese es el motivo de mover el sistema: si al reiniciar la máquina
los bots no vuelven solos, no habrás resuelto nada.

Guía completa: [`docs/DESPLIEGUE.md`](docs/DESPLIEGUE.md)

## Documentación

- [`docs/DESPLIEGUE.md`](docs/DESPLIEGUE.md) — mover el sistema a un equipo 24/7
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

Si nada responde: cierra las posiciones a mano en Binance. Ver
[`docs/RUNBOOK.md`](docs/RUNBOOK.md).

---

## Estructura

```
.
├── docker-compose.yml          # imagen oficial de Freqtrade
├── Makefile                    # atajos: setup, test, lookahead, backtest, data
├── user_data/
│   ├── config.dryrun.json      # papel, precios reales
│   ├── config.live.json        # dinero real — arranca detenido a propósito
│   ├── strategies/
│   │   ├── reglas_riesgo.py    # los límites. No optimizables. Verificados por tests.
│   │   ├── BaselineTrend.py    # la estrategia. Parámetros fijos.
│   │   └── BaselineTrendOpt.py # variante optimizable, SOLO para el walk-forward
│   └── data/                   # OHLCV + DATA_REPORT.md (no versionado)
├── tools/
│   ├── download_data.sh        # descarga OHLCV (dos pasadas: prepend + append)
│   ├── validate_data.py        # auditoría de calidad → DATA_REPORT.md
│   ├── run_backtest.py         # backtest reproducible + manifiesto
│   ├── walk_forward.py         # ventanas rodantes, mide sobreajuste
│   ├── report.py               # tabla comparable backtest / dry-run / live
│   ├── api_freqtrade.py        # cliente REST del bot
│   ├── kill_switch.py          # cierra todo y detiene
│   ├── watchdog.py             # heartbeat, límites, resumen diario
│   ├── estado.py               # qué está viendo el bot ahora mismo
│   ├── exportar_db.py          # saca el SQLite del volumen Docker al host
│   ├── filtro_ia.py            # filtro de contexto con Claude + noticias
│   ├── preparar_despliegue.sh  # verifica que una máquina está lista
│   ├── comparar_estrategias.py # tabla comparable de las cinco
│   ├── setup_telegram.py       # asistente de configuración de Telegram
│   └── entrada_journal.py      # entrada semanal del journal con datos reales
├── tests/                      # 92 tests
└── docs/
    ├── STRATEGY.md             # las reglas en español, sin código
    ├── RUNBOOK.md              # qué hacer cuando algo falla
    └── JOURNAL.md              # bitácora semanal (la llenas tú)
```

### Por qué la estrategia está partida en dos archivos

`BaselineTrend` no expone ningún parámetro de hyperopt, y eso es deliberado: es
la línea base contra la que se mide todo lo demás, y una referencia que se
optimiza deja de ser una referencia. Hay un test que falla si alguien le añade
parámetros optimizables.

`BaselineTrendOpt` hereda de ella y abre los parámetros de señal. Existe solo
para que el walk-forward tenga algo que optimizar y pueda medir cuánto
rendimiento se pierde fuera de muestra. Las reglas de riesgo se heredan sin
tocar en ambos casos.
