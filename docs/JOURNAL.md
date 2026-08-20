# Bitácora

La llena el humano, no el bot. Una entrada por semana durante el dry-run, y una
entrada por incidente siempre.

**Para qué sirve.** Dentro de tres meses no vas a recordar por qué cambiaste un
parámetro, ni qué pasó la semana que el sistema se comportó raro. Sin bitácora,
cada decisión se vuelve a tomar desde cero y con peor información. Con ella,
tienes el historial de tu propio criterio y puedes ver dónde te equivocaste.

**La sección más útil es «qué me sorprendió».** Lo que te sorprende es la
diferencia entre tu modelo mental del sistema y lo que el sistema hace de
verdad. Ahí es donde está lo que todavía no entiendes, y donde se esconden los
bugs y las malas decisiones futuras.

---

## Plantilla — entrada semanal

```markdown
## Semana del AAAA-MM-DD

**Fase:** dry-run / live / desarrollo
**Estado del bot:** corriendo sin interrupciones / reiniciado N veces / detenido

### Números

| | |
|---|---|
| Operaciones cerradas | |
| Ganadoras / perdedoras | |
| P&L de la semana | |
| P&L acumulado | |
| Drawdown máximo esta semana | |
| Equity actual | |

### Qué pasó

(Los hechos. Sin interpretación todavía.)

### Qué me sorprendió

(Lo que no esperabas: una operación que duró mucho más de lo previsto, un
stop que saltó donde no creías, una señal que no llegó cuando parecía obvia.
Si nada te sorprendió, escribe "nada" — pero piénsalo dos veces.)

### Qué cambiaría

(Ideas. NO cambios ejecutados. Durante el dry-run no se toca nada: cada ajuste
reinicia el reloj de validación. Se anotan para después.)

### Cómo va contra el backtest

| Métrica | Backtest | Dry-run | Desviación |
|---|---:|---:|---:|
| Expectativa por operación | | | |
| Win rate | | | |
| Profit factor | | | |

(Salida de `python tools/report.py --backtest <archivo> --dry-run <db>`.
El criterio del plan es < 15%. Por encima, hay una causa y hay que encontrarla
antes de seguir.)

### Decisiones tomadas

(Ninguna es una respuesta perfectamente válida y, durante el dry-run, la
esperada.)
```

---

## Plantilla — incidente

```markdown
## INCIDENTE AAAA-MM-DD HH:MM UTC

**Qué pasó:**
**Cómo me enteré:** (alerta de Telegram / lo vi por casualidad / no me enteré hasta después)
**Duración:**
**Impacto en dinero:**
**Posiciones afectadas:**

**Qué hice:**
1.
2.

**Causa raíz:**
(No "se cayó el bot". Por qué se cayó.)

**Qué evita que se repita:**
(Un cambio concreto en el código, la configuración o el procedimiento. Si la
respuesta es "estar más atento", no es una respuesta.)

**Sección del RUNBOOK que faltaba o estaba mal:**
```

---

## Antes de empezar — rellenar una sola vez

```markdown
## Punto de partida — AAAA-MM-DD

**Capital que puedo perder al 100% sin que afecte mi vida:** _____ USD

Esta cifra es el techo. No se sube después, ni cuando el sistema vaya bien
—sobre todo cuando el sistema vaya bien—, porque una racha buena es exactamente
el momento en que peor se calcula el riesgo.

**Cuánto tiempo estoy dispuesto a darle antes de evaluar:** _____ semanas

**Con qué resultado lo doy por fracasado y paro:**
(Escríbelo AHORA, en frío. Decidir esto con el sistema perdiendo dinero es
decidirlo con las peores condiciones posibles.)

**Qué espero aprender aunque pierda dinero:**
```

---

---

## Ejemplo de una semana rellena

> ### ⚠️ DATOS FICTICIOS — no son resultados de este bot
>
> Esto es solo para ver el formato con números dentro. **Nada de aquí ocurrió.**
> Las entradas reales van más abajo, bajo «Entradas», y sus números salen
> siempre de la base de datos con `python tools/entrada_journal.py`, nunca
> escritos a mano.
>
> Copiar cifras inventadas a la bitácora la inutiliza: dentro de un mes estarías
> comparando el dry-run contra una línea base que nunca existió, y la desviación
> que mide el criterio del 15 % dejaría de significar nada.

<details>
<summary><b>Ver el ejemplo</b> (semana ficticia, sistema perdiendo — que es lo esperado)</summary>

### Semana del 2026-09-14 · FICTICIA

**Fase:** dry-run
**Estado del bot:** corrió sin interrupciones, 0 reinicios

#### Números

| | |
|---|---|
| Operaciones cerradas | 4 |
| Ganadoras / perdedoras | 1 / 3 |
| P&L de la semana | −11.40 USDT (−1.14 %) |
| P&L acumulado | −23.80 USDT (−2.38 %) |
| Drawdown máximo esta semana | 1.62 % |
| Equity actual | 976.20 USDT |
| Posiciones abiertas al cierre | 1 |
| Operaciones desde el inicio | 9 |

| Par | Cierre | Resultado | Motivo de salida |
|---|---|---:|---|
| SOL/USDT | 2026-09-15 08:00 | −1.94 % | stop_loss |
| BTC/USDT | 2026-09-16 22:00 | +2.71 % | trailing_stop_loss |
| ETH/USDT | 2026-09-18 03:00 | −1.88 % | stop_loss |
| SOL/USDT | 2026-09-19 17:00 | −0.61 % | exit_signal |

#### Qué pasó

Cuatro operaciones, tres perdedoras. Las tres pérdidas fueron por stop o por
cruce bajista, todas entre −0.6 % y −1.9 %: ninguna se salió de la banda
esperada. La ganadora de BTC salió por trailing tras subir 2.7 %.

#### Qué me sorprendió

La entrada de ETH del día 17 llegó con el RSI en 68, casi en el límite de 70.
Entró y el precio se dio la vuelta en tres velas. Me pregunto si la banda
debería ser más estrecha por arriba — **pero no lo toco**, va a la lista.

Y algo que no esperaba: SOL generó la mitad de las señales él solo. Los tres
pares no contribuyen por igual.

#### Qué cambiaría

1. Probar RSI máximo en 65 en vez de 70.
2. Mirar si SOL merece un tratamiento distinto por su volatilidad.

Las dos van a la lista. **Ninguna se ejecuta durante el dry-run.**

#### Cómo va contra el backtest

| Métrica | Backtest | Dry-run | Desviación |
|---|---:|---:|---:|
| Expectativa por operación | −0.33 % | −0.36 % | −9.1 % |
| Win rate | 53.2 % | 44.4 % | −16.5 % ⚠ |
| Profit factor | 0.60 | 0.52 | −13.3 % |

El win rate se sale del 15 %, pero con 9 operaciones acumuladas eso es ruido:
una operación más o menos lo mueve 11 puntos. No se decide nada hasta pasar de
30.

#### Decisiones tomadas

Ninguna. Semana 3 de 4 del dry-run.

</details>

# Entradas

<!-- Las entradas nuevas van arriba, la más reciente primero. -->

## Semana del 2026-08-20 — construcción (T1–T10) y arranque del dry-run

**Fase:** desarrollo → dry-run
**Estado del bot:** en marcha desde el 2026-08-20 21:40 UTC, en Docker

### Números

| | |
|---|---|
| Operaciones cerradas | 0 |
| P&L acumulado | 0.00 USDT |
| Equity | 1.000,00 USDT (**simulados**) |
| Posiciones abiertas | 0 |

**Estado de la conexión, para que quede por escrito:**

| | |
|---|---|
| `dry_run` | `true` — ninguna orden llega al mercado |
| Claves de API de Binance | **ninguna configurada** (`.env` vacío) |
| Qué sí hace contra Binance | descargar precios en vivo por la API pública |
| Qué no puede hacer | consultar un saldo real o enviar una orden |
| Dinero en riesgo | **cero**, y no por disciplina sino porque no hay cuenta conectada |

El reloj de las 4 semanas de validación empieza hoy.

### Qué pasó

Se construyeron los diez tickets del plan. Sistema completo: datos auditados,
estrategia implementada y verificada sin sesgo de anticipación, 75 tests en
verde, backtest reproducible con costos reales, walk-forward, reportes,
configuración de dry-run, kill switch y vigilante.

### Qué me sorprendió

*(A rellenar por el humano tras revisar los reportes.)*

Tres cosas que aparecieron durante la construcción y conviene entender:

1. **`startup_candle_count = 200` no era suficiente.** Con 200 velas, la EMA(200)
   se desviaba un −0.63% de su valor convergido, porque una EMA nunca olvida del
   todo su valor inicial. Con 200 velas de arranque, el filtro de régimen del
   backtest no era el mismo que el de producción. Se subió a 600 (error: 0.004%).

2. **El primer backtest corrió sobre un solo par sin avisar.** Pasar
   `--pairs A --pairs B --pairs C` hace que argparse conserve solo el último.
   Freqtrade además limitó `max_open_trades` a 1 y reportó 87 operaciones donde
   debían ser 237. El backtest terminó sin error y con números de aspecto
   normal. Ahora el runner verifica los pares del resultado contra los pedidos.

3. **La baseline pierde dinero, como el plan anticipaba.** In-sample
   (2021-01 → 2024-06): 237 operaciones, −16.74%, profit factor 0.60. No pasa
   los criterios go/no-go y no debe llegar a dinero real.

4. **El criterio de degradación del walk-forward casi da un falso verde.** Con
   profit factor 0.42 en entrenamiento y 0.36 en prueba, la degradación sale del
   14% — por debajo del umbral del 40%. Leído sin contexto: «pasa». En realidad
   el sistema pierde dinero en las dos muestras y se degrada poco solo porque no
   se puede caer mucho desde el suelo.

   Ahora el reporte declara el criterio **no aplicable** cuando el profit factor
   de entrenamiento no supera 1.0, y hay un test que lo blinda. Un umbral de
   seguridad que da verde sobre un sistema roto es peor que no tener umbral.

5. **`/stop` no es lo que yo creía.** Deteniendo el bot, Freqtrade deja de
   procesar del todo: nadie mueve el trailing ni ejecuta los stops de las
   posiciones abiertas, y la API rechaza `forceexit` con «trader is not
   running». El primer kill switch falló contra un bot real por esto.

   El primitivo correcto es `/pause`: no abre posiciones nuevas pero sigue
   gestionando las abiertas. Se corrigieron el kill switch (pausar → cerrar →
   verificar → detener), el vigilante y el runbook.

6. **La media de degradaciones por ventana no significa nada con muestras
   pequeñas.** Los tramos de prueba de 3 meses tienen entre 1 y 17 operaciones.
   Una ventana de 3 operaciones dio −25.825% de degradación, y la media simple
   de las 18 ventanas salió −1.506%. Se añadió una degradación **agregada**
   sobre el conjunto de operaciones, que es la que decide el veredicto.

### Cómo va contra el backtest

| Métrica | In-sample (2021-01 → 2024-06) | Walk-forward fuera de muestra |
|---|---:|---:|
| Operaciones | 237 | 118 |
| Profit factor | 0.60 | 0.36 |
| Beneficio total | −16.74 % | negativo, ~−15 % del capital |
| Buy & hold BTC (mismo periodo) | +87.14 % | — |

### Decisiones tomadas

Ninguna sobre la estrategia. La baseline se deja exactamente como está: es la
vara de medir, y ajustarla ahora la invalidaría como punto de comparación.

Lo siguiente **no** es tocar parámetros. Es elegir **una** hipótesis de la
sección 9 del plan, probarla sola contra esta baseline con walk-forward, y
anotar aquí el resultado — gane o pierda.
