# Llevar el bot a una máquina 24/7

Guía para mover el sistema desde este Mac a un equipo que quede encendido
permanentemente. Sirve igual para otro PC, un servidor casero o un VPS.

---

## Lo que se mueve y lo que no

| | |
|---|---|
| **Sí se mueve** | el repositorio (código, configuraciones, documentación) |
| **Sí se mueve** | `.env` — pero **a mano y por canal seguro**, nunca por git |
| **No se mueve** | `user_data/data/` — son ~2 GB, se re-descargan en destino |
| **No se mueve** | la base de datos de operaciones — empieza limpia |

Las operaciones actuales no se llevan a propósito. Están contaminadas por los
apagones de estos días, y arrancar con historial sucio en la máquina donde vas a
medir de verdad no aporta nada.

---

## 1. Requisitos en la máquina destino

- **Docker** (Desktop en Windows/macOS, Engine en Linux)
- **git**
- **4 GB de RAM libres** — el sistema usa ~2,5 GB
- **20 GB de disco** — datos históricos y modelos

Verificar:

```bash
docker --version
docker compose version
docker info
```

---

## 2. Llevar el código

### Opción A — con git (recomendado)

En el Mac, sube el repositorio a GitHub como **privado**:

```bash
cd /Users/nandoski/Bot
gh repo create bot-trading --private --source=. --push
```

En la máquina destino:

```bash
git clone https://github.com/<tu-usuario>/bot-trading.git
cd bot-trading
```

> El repositorio **debe ser privado**. `.gitignore` ya excluye `.env`, pero un
> repositorio público expone tu estrategia y tu configuración.

### Opción B — copia directa

Desde el Mac, por red local:

```bash
rsync -av --exclude '.venv' --exclude 'user_data/data' \
      --exclude '.env' --exclude '*.sqlite' \
      /Users/nandoski/Bot/ usuario@ip-destino:~/bot-trading/
```

O con un USB, copiando la carpeta sin `.venv` ni `user_data/data`.

---

## 3. Las credenciales

**`.env` no viaja por git ni por chat.** Créalo en la máquina destino:

```bash
cp .env.example .env
```

Y rellena a mano:

| Variable | De dónde sale |
|---|---|
| `FREQTRADE__API_SERVER__PASSWORD` | genera una nueva: `python -c "import secrets;print(secrets.token_urlsafe(18))"` |
| `FREQTRADE__API_SERVER__JWT_SECRET_KEY` | `python -c "import secrets;print(secrets.token_hex(32))"` |
| `TELEGRAM_TOKEN` | el mismo de @BotFather |
| `TELEGRAM_CHAT_ID` | el mismo |
| `ANTHROPIC_API_KEY` | opcional, para el filtro de contexto |
| `BINANCE_API_KEY` / `SECRET` | **dejar vacías** — el dry-run no las necesita |

Para el token de Telegram sin que quede en el historial:

```bash
python tools/setup_telegram.py --pegar-token
```

> **Si el Mac y la nueva máquina van a correr a la vez, no compartas el token de
> Telegram.** Solo un cliente puede escuchar por token; se pelearían igual que
> se peleaban los cinco bots. Crea un segundo bot con @BotFather.

---

## 4. Descargar los datos

```bash
./tools/download_data.sh
python tools/validate_data.py
```

Tarda 10-20 minutos. Descarga 12 pares en 5m, 15m, 1h y 1d.

Comprueba que el reporte diga **CUMPLIDA** antes de seguir.

---

## 5. Arrancar

```bash
docker compose up -d
docker compose ps
```

Los siete contenedores deben quedar `Up`, y los cinco bots `(healthy)`.

Verificar:

```bash
python tools/estado.py
```

---

## 6. Que sobreviva a los reinicios

Es la razón de mover el sistema. Si no se configura esto, no habrás resuelto
nada.

### Linux

```bash
sudo systemctl enable docker
```

Los contenedores llevan `restart: unless-stopped`, así que vuelven solos.

### Windows

Docker Desktop → **Settings → General → Start Docker Desktop when you log in**.
Y en las opciones de energía, **«Nunca» suspender** con el equipo enchufado.

### macOS

Docker Desktop → **Settings → General → Start Docker Desktop when you sign in**.
Y Ajustes del Sistema → impedir la suspensión automática con la pantalla
apagada.

**Comprobación real:** reinicia la máquina y espera cinco minutos. Si
`python tools/estado.py` responde sin que hayas tocado nada, está bien
configurado. Si no, vuelve a este paso — es el que evita perder posiciones.

---

## 7. Acceso remoto a FreqUI

Los puertos están publicados **solo en `127.0.0.1`**, así que FreqUI solo se ve
desde la propia máquina. Es lo correcto y no hay que cambiarlo.

Para verlo desde otro equipo, usa un túnel SSH — nunca abras el puerto:

```bash
ssh -L 8080:127.0.0.1:8080 -L 8084:127.0.0.1:8084 usuario@ip-de-la-maquina
```

Con el túnel abierto, `http://localhost:8084` en tu portátil apunta al bot
remoto.

> **No cambies `127.0.0.1` por `0.0.0.0` en `docker-compose.yml`.** Eso expone
> el panel de control de tus bots a la red. La API permite cerrar posiciones y
> detener el sistema.

---

## 8. Comprobación diaria

```bash
python tools/estado.py --operaciones
```

Y con Telegram configurado, las notificaciones llegan solas: cada cierre, el
resumen diario, y aviso si algún bot deja de responder más de 10 minutos.

---

## Si algo falla

| Síntoma | Dónde mirar |
|---|---|
| Ningún bot responde | `docker compose ps` · `docker compose logs` |
| Un bot no arranca | `docker compose logs <nombre> --tail 50` |
| `disk I/O error` | [`RUNBOOK.md`](RUNBOOK.md) — sección de SQLite |
| Telegram no contesta | solo el vigilante responde; ver `RUNBOOK.md` |
| Sin operaciones en días | `python tools/estado.py --bot <nombre>` para ver señales |

El [`RUNBOOK.md`](RUNBOOK.md) cubre los fallos conocidos con su diagnóstico.
