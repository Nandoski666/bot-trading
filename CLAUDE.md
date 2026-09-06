# Contexto para Claude Code

Bot de trading algorítmico en cripto spot, construido sobre Freqtrade siguiendo
un plan escrito por el usuario (`~/Downloads/plan-bot-trading.md` en la máquina
original; sus reglas están resumidas abajo).

**Lee esto antes de tocar nada.** Contiene hallazgos que costaron días de
medición y varios fallos silenciosos ya diagnosticados. Repetirlos es fácil.

---

## Estado actual

Todo en **dry-run** (dinero simulado). Nunca ha operado con dinero real y no
debe hacerlo hasta cumplir los criterios go/no-go de la sección 4 del plan.

**Ocho estrategias**, todas heredan el riesgo de `EstrategiaBase`:

| Estrategia | Velas | Resultado backtest |
|---|---|---|
| `BaselineTrend` | 1h | −51 % (12 pares) |
| `Orochi` | 1h | −21 % |
| `ReversionRSI` | 1h | −10 % |
| `RupturaDonchian` | 1h | −82 % |
| `MomentumMultiple` | 1h | −98 % |
| `*Rapida` (5 variantes) | 5m | −91 % a −98 % |
| `AprendizModelo` (FreqAI) | 5m | −75 % |
| `TendenciaMedia` | 4h | −35 % |
| **`TendenciaLarga`** | **1d** | **−2 %, DD 7,3 %** ← la mejor |

Ninguna gana a comprar y mantener BTC (+270 %, DD 49,5 %).

---

## Los cuatro hallazgos que importan

### 1. El coste por operación decide casi todo

Medido sobre BTC/USDT con 0,30 % de coste por operación completa:

| Velas | ATR medio | **Coste / ATR** |
|---|---:|---:|
| 5m | 0,17 % | **172 %** |
| 1h | 0,84 % | 36 % |
| 4h | 1,71 % | 18 % |
| 1d | 4,41 % | 7 % |

En 5 minutos se paga más de lo que la vela se mueve. Las estrategias rápidas no
eran malas — eran aritméticamente imposibles. **No propongas timeframes cortos
sin recalcular esta tabla.**

### 2. Las señales no tienen ventaja, ni siquiera sin comisiones

El bruto por operación (quitando el 0,30 %) es negativo en las ocho. El problema
no son los costes: es que indicadores públicos sobre los 12 pares más líquidos
no anticipan nada. **Añadir más indicadores o más IA encima no lo arregla — ya
se probó.**

### 3. La regla de riesgo limita el retorno por aritmética

```
riesgo 0,5 % / stop 2×ATR (8,8 % en diario) = posición de 5,7 % del capital
× 3 posiciones = 17 % máximo invertido
exposición real medida = 4,8 %
```

Con 4,8 % de exposición media en un mercado que subió 270 %, el techo era ~13 %.
**La regla que protege el capital es la que impide capturar el mercado.**
Cambiarla es decisión del usuario, no del código.

### 4. El trailing decide el perfil de resultados

Con trailing estrecho las ganadoras salen a +4,5 % y las perdedoras a −5,3 %
(ratio 0,85 = muerte para un seguidor de tendencia). Ensanchándolo: +7,1 % /
−5,3 % = 1,33. **Ajusta el trailing al timeframe.**

---

## Reglas que no se tocan

De la sección 3 del plan, en `user_data/strategies/reglas_riesgo.py`:

| | |
|---|---|
| Riesgo por operación | 0,5 % del equity |
| Posiciones simultáneas | máximo 3 |
| Pérdida diaria | 3 % → pausa |
| Drawdown total | 10 % → kill switch |
| Apalancamiento | 0, solo spot |

Hay tests que fallan si una estrategia las redefine. **No las cambies aunque el
backtest mejore** — eso es exactamente lo que el plan prohíbe.

**Sí son ajustables por estrategia** (y hay tests que prueban por qué no son
riesgo): `atr_multiplicador_stop`, `atr_activacion_trailing`,
`atr_distancia_trailing`. Ensanchar el stop no aumenta el riesgo porque el
tamaño se reduce en proporción.

---

## Trampas ya pisadas — no repetir

| Síntoma | Causa real |
|---|---|
| Backtest con 0 operaciones, sin error | comentarios `//_` en `data_split_parameters` de FreqAI → llegan como kwargs a sklearn |
| `lookahead-analysis: has_bias Yes` con `biased_indicators` vacía | **falso positivo** del límite de 3 posiciones. Ver `RUNBOOK.md` |
| Estrategia corriendo en el timeframe equivocado | `timeframe` en `config.json` **pisa** al de la clase. No lo pongas ahí |
| `sqlite3.OperationalError: disk I/O error` | SQLite en bind mount de macOS + suspensión. La BD va en volumen Docker |
| `Conflict: terminated by other getUpdates` | varios bots con el mismo token de Telegram. Lo lleva el vigilante |
| 87 operaciones donde debían ser 237 | `--pairs A --pairs B` conserva solo la última. Usa `--pairs A B C` |
| Todo parado sin avisar | el equipo se suspendió. Docker se congela con él |

---

## Comandos

```bash
docker compose up -d                    # arrancar los 5 bots + vigilante + filtro IA
python tools/estado.py                  # qué está pasando ahora
python tools/estado.py --operaciones    # ganadas y perdidas
python tools/kill_switch.py --confirm   # cerrar todo y parar
python -m pytest tests/ -q              # 478 tests
./tools/preparar_despliegue.sh          # verificar la máquina antes de arrancar
```

Backtest reproducible con costes:
```bash
python tools/run_backtest.py --window in-sample
python tools/comparar_estrategias.py
```

FreqAI necesita su config aparte (si va en el principal rompe las demás
estrategias):
```bash
freqtrade backtesting --config user_data/config.dryrun.json \
    --config user_data/config.freqai.json \
    --strategy AprendizModelo --freqaimodel LightGBMRegressor
```

---

## La capa de IA

`tools/filtro_ia.py` consulta a Claude o a Groq (elige según la clave en `.env`)
con el estado del mercado y titulares RSS de CoinDesk, Cointelegraph y Decrypt.

**Tres invariantes con tests que fallan si se rompen:**

1. **Solo puede vetar entradas**, nunca provocarlas. Un LLM generando señales no
   es determinista ni backtesteable.
2. **Se ignora por completo en backtest.** Si afectara, ningún resultado
   histórico sería reproducible.
3. **Falla abierto.** Sin clave, con la API caída o el veredicto caducado, se
   opera.

Groq gratis: los modelos `compound` (con búsqueda web) dan 413 en el tier
gratuito; se usa `openai/gpt-oss-120b` y las noticias se traen por RSS. Su modo
estricto exige `required` con todas las propiedades.

---

## Cómo trabajar aquí

- **Mide, no opines.** Cada afirmación sobre rendimiento sale de un backtest con
  costes. Si no está medido, dilo.
- **Un cambio a la vez**, validado con walk-forward contra la baseline.
- **Reporta las pérdidas igual que las ganancias.** El valor del proyecto es la
  máquina de medir, no que gane.
- **Si un resultado parece demasiado bueno, hay un bug.** Ha pasado tres veces.
- Los tests deben quedar en verde en cada commit.
- Comentarios y documentación **en español**, como todo el proyecto.

## Lo que NO hay que hacer

- Prometer rentabilidad. Ocho estrategias medidas, ninguna funciona.
- Bajar el timeframe para "ver más operaciones" sin mirar la tabla de costes.
- Ajustar parámetros hasta que el backtest se vea bien. Eso es sobreajuste y el
  walk-forward existe para detectarlo.
- Meter claves API en archivos por el usuario. Usa `tools/pegar_clave_ia.py` y
  `tools/setup_telegram.py --pegar-token`, que las piden ocultas.
