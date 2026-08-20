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

# Entradas

<!-- Las entradas nuevas van arriba, la más reciente primero. -->

## Semana del 2026-08-20 — construcción (T1–T10)

**Fase:** desarrollo
**Estado del bot:** todavía no ha corrido en dry-run

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
