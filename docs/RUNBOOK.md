# Runbook — qué hacer cuando algo falla

Documento de emergencia. Está escrito para leerlo con prisa y sin contexto:
cada sección empieza por el síntoma, no por la causa.

**Regla general:** ante la duda, **parar**. Un bot detenido no pierde dinero.
Un bot funcionando mal, sí.

---

## Comandos de emergencia

```bash
python tools/kill_switch.py --confirm
```

Cierra todas las posiciones a mercado y detiene el bot.

```bash
python tools/kill_switch.py --solo-detener
```

**Pausa** el bot: deja de abrir posiciones nuevas, pero sigue gestionando las
abiertas — trailing y stops activos. Es la opción correcta cuando quieres parar
pero no realizar pérdidas en ese momento.

**Desde Telegram** (funciona aunque no tengas acceso al servidor):

| Comando | Qué hace |
|---|---|
| `/pause` | Deja de abrir. **Sigue gestionando** las abiertas. |
| `/forceexit all` | Cierra todas las posiciones a mercado |
| `/stop` | Para el bot **del todo** — ver el aviso de abajo |
| `/status` | Posiciones abiertas y su P&L |
| `/profit` | Resumen de resultados |
| `/start` | Reanuda la apertura de posiciones |

> ### `/pause` y `/stop` no son lo mismo
>
> - **`/pause`** → estado PAUSED. El bot sigue su ciclo: vigila stops, mueve el
>   trailing y ejecuta las salidas. Solo deja de entrar.
> - **`/stop`** → estado STOPPED. El bot deja de procesar. **Las posiciones
>   abiertas quedan sin gestionar:** nadie mueve el trailing ni ejecuta el stop.
>   En live solo sobreviven porque `stoploss_on_exchange` deja la orden puesta
>   en Binance. En dry-run quedan completamente desatendidas.
>
> Además, con el trader detenido la API **rechaza `forceexit`**
> (`trader is not running`). Por eso `kill_switch.py` pausa primero, cierra
> después, y solo detiene al final, cuando ya no queda nada que gestionar.
>
> Regla práctica: **usa `/pause` casi siempre.** `/stop` solo cuando no haya
> posiciones abiertas.

**Si nada de esto responde:** entra a Binance por la web y cierra las
posiciones a mano. Es siempre la opción disponible. No esperes a que el bot
vuelva.

---

## Diagnóstico rápido

```bash
docker compose ps                      # ¿el contenedor está vivo?
docker compose logs --tail 100         # ¿qué fue lo último que hizo?
python tools/watchdog.py --once        # heartbeat, límites, estado
python tools/estado.py                 # qué está viendo el bot y qué le falta
```

`estado.py` es el que responde «¿está analizando o está colgado?»: enseña los
valores de los indicadores que el bot acaba de calcular y cuál de las cuatro
condiciones de entrada bloquea cada par.

FreqUI: <http://localhost:8080>

---

## Síntoma: el exchange rechaza las órdenes

**En los logs:** `InsufficientFunds`, `Order would trigger immediately`,
`MIN_NOTIONAL`, `Invalid API-key, IP, or permissions`.

### Causas por orden de probabilidad

**1. La orden no llega al mínimo del exchange.** Binance exige un notional
mínimo (unos 5–10 USDT según el par). Con capital pequeño y un stop ancho, el
tamaño calculado puede quedar por debajo.

*Es comportamiento correcto:* la estrategia prefiere no entrar antes que
arriesgar más del 0.5%. Si pasa a menudo, el capital es demasiado pequeño para
estas reglas de riesgo. La respuesta **no** es subir el riesgo por operación.

**2. Saldo insuficiente.** Hay USDT bloqueado en órdenes abiertas, o el saldo
real es menor que el que cree el bot.

```bash
docker compose restart      # el bot reconcilia el saldo al arrancar
```

**3. La API key perdió permisos o cambió tu IP.**

- ¿La whitelist de IP sigue coincidiendo con la IP del VPS? Los proveedores la
  cambian tras algunas migraciones.
- Binance caduca las API keys tras 90 días sin uso.
- Verifica que sigue teniendo **solo trading spot** y **retiros deshabilitados**.

**4. El par se suspendió.** Binance detiene pares por mantenimiento. Míralo en
su página de estado. Si es prolongado, sácalo del `pair_whitelist`.

### Qué hacer

1. Lee el mensaje de error completo en los logs. Es específico.
2. Si es de permisos o IP: `--solo-detener`, arregla la key, reinicia.
3. Si es de mínimos: no es un error. Anótalo en el journal.
4. **Nunca** subas el riesgo por operación para que las órdenes pasen el mínimo.

---

## Síntoma: se cayó la conexión

Freqtrade reconecta solo. Reintenta con espera creciente y no duplica órdenes.

### Cuándo preocuparse

- **Menos de 5 minutos:** normal. No hagas nada.
- **Más de 10 minutos:** el watchdog te avisa por Telegram. Comprueba si es tu
  red o Binance (status.binance.com).
- **Con posiciones abiertas y caída larga:** el riesgo real es que el precio
  toque el stop y el bot no esté para ejecutarlo.

### La protección que hay que tener puesta

En `config.live.json` está activado `stoploss_on_exchange`: el stop se coloca
**en Binance**. Si el bot desaparece, la protección sigue puesta.

En dry-run está desactivado porque no hay órdenes reales que colocar.

> Si operas en vivo con `stoploss_on_exchange: false`, una caída del bot deja las
> posiciones completamente desprotegidas. No lo hagas.

---

## Síntoma: el bot está muerto y hay una posición abierta

El caso peor. Actúa en este orden.

### 1. ¿Hay stop puesto en el exchange?

Entra a Binance → Órdenes → Órdenes abiertas. Si ves una orden stop-limit del
par en cuestión, la posición está protegida. Tienes tiempo.

### 2. Si no hay stop, decide ahora

Calcula el stop que debería tener: `precio_de_entrada − 2 × ATR`. El precio de
entrada está en Binance (historial de operaciones) y en la base de datos:

```bash
sqlite3 user_data/tradesv3.dryrun.sqlite \
  "SELECT id, pair, open_date, open_rate, amount, is_open FROM trades WHERE is_open = 1;"
```

Dos opciones, las dos válidas:

- **Colocar el stop a mano en Binance** y luego arreglar el bot con calma.
- **Cerrar la posición a mercado** y arrancar limpio. Más simple, y en una
  emergencia lo simple gana.

### 3. Recuperar el bot

```bash
docker compose logs --tail 200 > /tmp/crash.log   # ANTES de reiniciar
docker compose up -d
```

Guarda los logs primero. Si reinicias sin ellos, pierdes la única evidencia de
por qué se cayó y volverá a pasar.

Freqtrade recupera las posiciones abiertas desde la base de datos al arrancar y
sigue gestionándolas.

### 4. Si la base de datos se corrompió

```bash
sqlite3 user_data/tradesv3.dryrun.sqlite "PRAGMA integrity_check;"
```

Si falla: cierra todo a mano en Binance, mueve el archivo a un lado, y arranca
de cero. **No intentes reparar la base de datos con posiciones abiertas.**

---

## Síntoma: hay que reiniciar el VPS

### Reinicio planificado

```bash
python tools/kill_switch.py --solo-detener   # deja de abrir posiciones
# espera a que se cierren las abiertas, o ciérralas a mano
python tools/kill_switch.py --confirm
docker compose down
sudo reboot
```

Al volver:

```bash
cd ~/trading-bot
docker compose up -d
docker compose logs -f          # confirma que arranca limpio
python tools/watchdog.py --once
```

### Reinicio inesperado

Con `restart: unless-stopped` en el compose, Docker levanta el bot solo cuando
el demonio arranca. Verifica que Docker tiene arranque automático:

```bash
sudo systemctl enable docker
```

Tras el arranque, **confirma siempre** que el bot recuperó su estado:

```bash
docker compose logs | grep -i "open trades\|Reloading"
```

---

## Síntoma: hay que rotar las API keys

Hazlo si sospechas una filtración, cada 90 días, o al cambiar de VPS.

**Con una filtración, el orden importa: revocar primero, preguntar después.**

### 1. Parar

```bash
python tools/kill_switch.py --confirm
docker compose down
```

### 2. Revocar la vieja en Binance

Perfil → Gestión de API → eliminar la clave. Confirma que ya no aparece.

### 3. Crear la nueva

- Permisos: **solo** «Enable Spot & Margin Trading»
- **Retiros: DESHABILITADOS**
- Restricción de IP: la IP fija del VPS

### 4. Actualizar `.env` y arrancar

```bash
nano .env          # BINANCE_API_KEY, BINANCE_API_SECRET
docker compose up -d
docker compose logs -f
```

### 5. Verificar

```bash
docker compose logs | grep -i "balance\|authenticat"
```

> Si la clave estuvo expuesta en un repositorio, un chat o una captura:
> **revócala igualmente**, aunque parezca que no pasó nada. Los bots que
> rastrean claves filtradas tardan minutos, no días.

---

## Síntoma: `disk I/O error` de SQLite, o el bot deja de latir a ratos

**En Telegram:** `🟠 Bot sin latido — el proceso responde pero no está
procesando velas`, a veces varias veces por noche.

**En los logs:** `sqlite3.OperationalError: disk I/O error`, o
`unable to open database file`.

### Causa: el Mac se suspende

macOS entra en *Maintenance Sleep* aunque esté enchufado y con la tapa abierta.
Cuando lo hace, la máquina virtual de Docker se congela con él — el bot y el
vigilante incluidos. Al despertar, el bot lleva 15-20 minutos sin procesar
velas.

Comprobarlo:

```bash
pmset -g log | grep -E "Entering Sleep|Wake from" | tail -20
```

Compara esas horas con los huecos del heartbeat (recuerda que el contenedor
loguea en UTC).

### Por qué rompía la base de datos

SQLite mantiene el archivo abierto durante toda la vida del proceso. En un
*bind mount* de macOS, esos descriptores quedan inválidos tras la suspensión, y
a partir de ahí **toda** operación contra la base falla — aunque
`PRAGMA integrity_check` siga diciendo `ok`. El archivo está sano; el proceso ya
no puede escribirlo.

Con una posición abierta, eso es un bot incapaz de registrar lo que hace.

**Ya está resuelto:** la base de datos vive en un volumen nombrado de Docker
(`db:/freqtrade/db`), sobre el ext4 de la máquina virtual, donde los bloqueos
funcionan y sobreviven a la suspensión. Si vuelves a ver `disk I/O error`,
comprueba que el `docker-compose.yml` no ha vuelto al *bind mount*.

Para leer la base desde el host:

```bash
python tools/exportar_db.py
```

`report.py` y `entrada_journal.py` la exportan solas antes de leer.

### Qué hacer si pasa igualmente

```bash
docker compose restart freqtrade    # handle nuevo, se recupera al instante
```

### Cómo evitar la suspensión

**Apaño para el dry-run** — mantener el Mac despierto mientras corre el bot:

```bash
caffeinate -dimsu &
```

Evita la suspensión por inactividad. **No evita la de cerrar la tapa**: si
cierras el portátil, se duerme igual.

**La solución de verdad es un VPS.** Es lo que dice el plan y esta noche es la
demostración: un portátil no es una máquina de 24/7. Con dinero real, cada
suspensión es una ventana en la que nadie mueve el trailing ni ejecuta un stop.
Hetzner o DigitalOcean, ~5 USD/mes.

### Qué hace el vigilante ahora

Distingue los dos casos por sí solo: si su propio ciclo de espera tardó mucho
más de lo pedido, sabe que se paró la máquina entera y avisa de
**«el equipo estuvo suspendido»** en lugar de «bot atascado». Si el bot se cuelga
sin que haya habido suspensión, sigue avisando como fallo.

---

## Síntoma: se filtró el token de Telegram

Cuenta como filtración si el token apareció en **cualquier** sitio que no sea
tu `.env`: un chat, una captura, un pegado en un issue, el historial del shell,
un log, un commit.

### Qué puede hacer quien lo tenga

Leer todo lo que le llegue a tu bot y escribir en su nombre. Como el bot está
autorizado a controlar Freqtrade por Telegram, eso incluye mandarle `/forceexit
all`, `/stop` o `/start`. **No puede sacar dinero** —los retiros no pasan por
aquí—, pero sí puede cerrarte posiciones o dejar el bot parado sin que te
enteres.

### Revocar (30 segundos)

1. En Telegram, a **@BotFather**: `/revoke`
2. Elige tu bot.
3. Te da un token nuevo. **El anterior deja de funcionar al instante.**

### Poner el nuevo sin volver a filtrarlo

```bash
python tools/setup_telegram.py --pegar-token
```

Lo pide oculto: no se ve al escribir y no entra en `~/.zsh_history`. Después:

```bash
python tools/setup_telegram.py
docker compose up -d --force-recreate
```

> **No pases un token como argumento de un comando.** `export TOKEN=8123...` o
> `--token 8123...` quedan en claro en el historial del shell, que casi nadie
> limpia nunca.

### Si sospechas que alguien lo usó

Mira `docker compose logs freqtrade | grep -i telegram`: los comandos recibidos
quedan registrados. Compara con lo que hiciste tú.

---

## Síntoma: el dry-run no se parece al backtest

El criterio del plan es una desviación menor al 15%. Por encima, hay una causa
concreta y hay que encontrarla.

```bash
python tools/report.py \
  --backtest user_data/backtest_results/<archivo>.zip \
  --dry-run user_data/tradesv3.dryrun.sqlite
```

### Causas por orden de probabilidad

**1. Slippage real mayor que el simulado.** El backtest asume 0.05%. En
mercados agitados una orden a mercado puede llenarse mucho peor. Compara el
precio de entrada real con el cierre de la vela de la señal.

**2. Órdenes que no se llenaron.** El backtest asume que toda señal se ejecuta.
En vivo, `unfilledtimeout` cancela las que no llenan en 10 minutos.

**3. Menos operaciones que en el backtest.** Suele ser bueno: las protecciones
(`CooldownPeriod`, `StoplossGuard`) están bloqueando entradas que el backtest sí
tomó.

**4. Sesgo de anticipación que el análisis no detectó.** El caso más grave.
Señal: el dry-run es sistemáticamente peor, no puntualmente.

```bash
make lookahead
.venv/bin/freqtrade recursive-analysis --config user_data/config.dryrun.json \
    --strategy BaselineTrend --datadir user_data/data -p BTC/USDT
```

> Una desviación grande **no se arregla ajustando parámetros**. Se diagnostica.
> Ajustar sin entender la causa reinicia el reloj de validación y no arregla nada.

---

## Síntoma: saltó un límite de riesgo

### Pérdida diaria (3%)

El bot queda **pausado**: no abre posiciones nuevas, pero sigue gestionando las
abiertas (trailing y stop activos).

**No se reactiva solo, y es a propósito.** Antes de `/start`:

1. Mira las operaciones del día: ¿pérdidas normales o algo se rompió?
2. ¿Fue el mercado o fue el sistema? Un día de −3% en un desplome general es
   distinto de un −3% con el mercado plano.
3. Anótalo en `docs/JOURNAL.md`.

### Drawdown total (10%) — kill switch

Todo cerrado, bot apagado. **El watchdog queda enclavado**: no se rearma solo.

Para volver a operar:

1. Diagnostica. Un 10% desde el máximo significa que el sistema dejó de
   funcionar, o que el régimen de mercado cambió, o que hay un bug.
2. Escribe la conclusión en el journal.
3. Borra el estado del watchdog:
   ```bash
   rm user_data/watchdog_estado.json
   ```
4. Arranca el bot.

> El paso 1 no es opcional. Reiniciar sin diagnóstico repite exactamente la
> misma pérdida, y la segunda vez duele más porque ya sabías.

---

## Antes de cualquier cambio en producción

1. ¿Están los tests en verde? `make test`
2. ¿Sigue sin sesgo de anticipación? `make lookahead`
3. ¿Está anotado en `docs/JOURNAL.md` qué cambias y por qué?
4. ¿Es **un solo** cambio?
5. ¿Aceptas que el reloj de validación vuelve a cero?

Si alguna respuesta es no, no es el momento de tocar producción.
