# La estrategia, en español y sin código

Este documento explica **qué** hace el sistema y **por qué**. Si no puedes
explicarle a alguien lo que hay aquí, no deberías operarlo con dinero. Cuando
el bot haga algo raro, este documento es la referencia contra la que comparar.

---

## Idea de fondo

Es un sistema **seguidor de tendencia**. No intenta predecir nada. Su apuesta,
que es una hipótesis sobre el mercado y no una certeza, es esta:

> Los precios de las criptomonedas grandes se mueven a rachas. Cuando arranca
> una racha alcista, tiende a durar lo suficiente como para que entrar tarde y
> salir tarde deje un margen — siempre que las salidas malas se corten pronto.

De ahí se deriva todo lo demás. Un sistema así **pierde la mayoría de las
veces**: acumula muchas pérdidas pequeñas esperando las pocas rachas grandes
que las pagan todas. Un win rate del 45% con esta estructura puede ser
rentable; uno del 60% con pérdidas grandes, no.

**Consecuencia práctica:** si algún día decides "mejorar" el sistema cerrando
antes las ganadoras para subir el win rate, lo estarás rompiendo. Las pocas
operaciones largas son el negocio entero.

---

## Universo y ritmo

| | |
|---|---|
| **Qué se opera** | BTC/USDT, ETH/USDT, SOL/USDT |
| **Dónde** | Binance, mercado **spot** (sin apalancamiento) |
| **Cada cuánto se mira** | Velas de 1 hora |
| **Dirección** | Solo compras. Nunca ventas en corto. |

**Por qué solo tres pares:** son los más líquidos, tienen histórico largo y sus
comisiones y slippage son predecibles. Añadir pares pequeños multiplica las
oportunidades aparentes y también el ruido, el slippage real y el riesgo de
listados que desaparecen.

**Por qué 1 hora:** en marcos más cortos las comisiones se comen el margen
(0.30% por operación completa contra movimientos de 0.5% no deja nada). En
marcos más largos hay tan pocas operaciones que se tardan años en saber si el
sistema funciona.

**Por qué solo largos:** en spot no se puede vender lo que no se tiene. Hacer
cortos exige margen o futuros, que es apalancamiento, que está descartado.

---

## Cuándo compra

Las cuatro condiciones tienen que cumplirse **en la misma vela ya cerrada**.
Si falta una, no hay entrada.

### 1. La media de 20 cruza por encima de la media de 50

Una media móvil exponencial (EMA) es el precio promedio reciente, dando más
peso a lo más nuevo. La de 20 horas reacciona rápido; la de 50, despacio.

Cuando la rápida cruza por encima de la lenta, el precio de las últimas horas
se ha puesto por encima del de los últimos días: **algo cambió de ritmo**.

Es un **evento**, no un estado. Dispara solo en la hora del cruce. Si fuese un
estado ("la rápida está por encima"), el bot intentaría comprar cada hora
durante toda la tendencia.

> **Lo que este filtro no hace:** no predice. Confirma un cambio que ya
> ocurrió. Siempre se entra tarde. Eso es el precio de no entrar en cada
> movimiento falso.

### 2. El precio está por encima de la media de 200

Filtro de régimen. La EMA de 200 horas (unos 8 días) marca la dirección de
fondo. Solo se compra por encima de ella.

**Es el filtro que más trabaja.** Los cruces alcistas ocurren constantemente,
también dentro de mercados bajistas, donde casi siempre son rebotes que fallan.
Este filtro los descarta en bloque. Reduce mucho el número de operaciones y
elimina la peor clase de ellas.

### 3. El RSI está entre 40 y 70

El RSI mide la fuerza del movimiento reciente en una escala de 0 a 100. Recorta
por los dos lados:

- **Por debajo de 40:** el movimiento no tiene fuerza. El cruce probablemente
  es deriva, no impulso.
- **Por encima de 70:** sobrecompra. Se llega tarde a un movimiento que ya
  corrió; el recorrido que queda es poco y el retroceso, probable.

### 4. El volumen supera su media de 20 horas

Un cruce de medias con volumen bajo es movimiento de precio sin dinero detrás.
Suele deshacerse. Este filtro exige que haya participación real.

---

## Cuándo vende

Tres salidas independientes. La primera que ocurra cierra la posición.

### 1. La media de 20 cruza por debajo de la media de 50

El simétrico de la entrada: el impulso que motivó la compra se ha agotado.

### 2. Stop inicial — 2 ATR por debajo de la entrada

El **ATR** (rango verdadero medio) mide cuánto se mueve el par en una hora
típica. Es la unidad natural de "distancia" de cada mercado.

El stop se coloca a **2 ATR** por debajo del precio de entrada.

**Por qué en ATR y no en porcentaje fijo:** un stop del 3% es enorme para un
BTC tranquilo y ridículo para un SOL agitado. En el primer caso se arriesga de
más; en el segundo, salta por ruido normal antes de que la idea tenga
oportunidad. El ATR ajusta la distancia al mercado del momento, solo.

**El ATR se congela en la entrada.** Se guarda el valor de la vela que generó
la señal y no se vuelve a tocar. Si se recalculara, una subida de volatilidad
alejaría el stop de una posición ya abierta — y su pérdida máxima pasaría a ser
mayor que el 0.5% con el que se dimensionó.

### 3. Trailing stop — se arma en +1.5 ATR, arrastra a 1 ATR

Mientras el precio no haya subido 1.5 ATR, solo existe el stop inicial.

Cuando el **máximo alcanzado** supera entrada + 1.5 ATR, el stop pasa a
colocarse 1 ATR por debajo de ese máximo, y sube con él.

El hueco entre 1.5 y 1.0 no es arbitrario: al armarse, el stop queda 0.5 ATR
**por encima** de la entrada. La operación ya no puede perder. Si el trailing se
armara a la misma distancia a la que arrastra, se activaría justo en el punto de
cerrar y sacaría la posición al instante.

**Sigue al máximo, no al precio actual.** Si el precio corrige, el stop se queda
donde está. Un stop que baja con el precio no protege nada.

### Lo que NO hay

- **No hay objetivo de beneficio fijo.** Un ROI del 5% cortaría exactamente las
  operaciones largas que sostienen el sistema.
- **No se promedia a la baja. Nunca.** Comprar más de algo que va en contra
  convierte una pérdida acotada en una sin límite. Es la forma más habitual de
  perderlo todo con un sistema que "casi nunca pierde".

---

## Cuánto arriesga en cada operación

**Regla: 0.5% del capital total por operación.**

No 0.5% del precio ni del tamaño de la posición: **del capital**. Si el stop se
ejecuta, se pierde el 0.5% de la cuenta. Siempre lo mismo, en cualquier par y
en cualquier régimen de volatilidad.

El tamaño se despeja de ahí:

```
distancia al stop (%) = 2 × ATR / precio de entrada
tamaño de la posición = (capital × 0.5%) / distancia al stop (%)
```

Un ejemplo con 1.000 USDT:

| Situación | Distancia al stop | Tamaño | Pérdida si salta |
|---|---:|---:|---:|
| Mercado tranquilo | 2% | 250 USDT | 5 USDT |
| Mercado normal | 5% | 100 USDT | 5 USDT |
| Mercado agitado | 10% | 50 USDT | 5 USDT |

La columna de la derecha es siempre la misma. **Eso es la gestión de riesgo**:
no evitar pérdidas, sino hacerlas iguales y conocidas.

**Hay un tope.** Con volatilidad muy baja la fórmula pide posiciones enormes
(un stop del 0.3% pediría el 167% del capital). Se limita a un tercio del
capital, para que quepan las tres posiciones. El tope solo puede reducir el
riesgo, nunca aumentarlo.

---

## Los límites que no se negocian

| Límite | Valor | Qué pasa al tocarlo |
|---|---|---|
| Riesgo por operación | 0.5% | — |
| Posiciones a la vez | 3 | No se abren más |
| Pérdida en un día | 3% | Pausa: deja de abrir, sigue gestionando lo abierto. Solo se reactiva a mano. |
| Caída desde el máximo | 10% | Kill switch: cierra todo y se apaga |
| Apalancamiento | 0 | — |

Estas cifras están en el código (`user_data/strategies/reglas_riesgo.py`),
verificadas por tests, y **ningún proceso de optimización puede tocarlas**. Hay
un test que falla si alguien las convierte en parámetros ajustables.

La razón es concreta: cualquier optimizador que pueda mover el riesgo por
operación descubrirá que arriesgar más mejora el resultado del backtest — porque
en el pasado ya sabemos que la cuenta no quebró. En el futuro no existe esa
garantía.

**Por qué 0.5% y no el 2% que repite todo el mundo:** con 0.5% hacen falta unas
20 pérdidas seguidas para llegar al límite del 10%. Con 2%, bastan 5. Cinco
pérdidas seguidas no son una anomalía; son un martes.

**Por qué tres posiciones y no más:** BTC, ETH y SOL se mueven casi a la vez.
Tres posiciones simultáneas no son tres apuestas: son una sola apuesta
direccional multiplicada por tres. Contarlas como diversificación es engañarse.

---

## Lo que esta estrategia no es

Conviene decirlo claro:

- **No es una estrategia ganadora.** Es una línea base estándar y honesta. Su
  función es servir de vara de medir: cualquier idea futura tiene que ganarle
  en métricas ajustadas por riesgo, o no vale la pena.
- **En el backtest in-sample pierde dinero** (ver el reporte de métricas). Eso
  no es el fracaso del proyecto. El proyecto es la máquina de probar ideas; la
  primera idea casi nunca es la buena.
- **No usa nada exótico.** EMA, RSI, ATR y volumen. Cuatro indicadores que
  llevan décadas publicados. Añadir más no daría más precisión: daría más
  sobreajuste.

---

## Lo que se puede tocar y lo que no

**Se puede probar** (una cosa cada vez, con walk-forward, contra la baseline):

- Los periodos de las medias y los límites de la banda de RSI
- La distancia del stop y del trailing en ATR
- El universo de pares
- Filtros nuevos, si vienen con una hipótesis explícita sobre el mercado

**No se toca:**

- Los límites de riesgo de la tabla de arriba
- El principio de un solo cambio a la vez
- Los criterios go/no-go

> Probar cinco cambios juntos y ver que mejoró no te dice cuál funcionó. Te dice
> que tuviste suerte con la combinación en ese periodo concreto.
