#!/usr/bin/env bash
#
# Comprueba que la maquina actual esta lista para desplegar el bot.
#
# Se ejecuta EN LA MAQUINA DESTINO, antes de arrancar nada. Verifica lo que en
# este proyecto ya ha fallado de verdad al menos una vez: Docker parado, RAM
# insuficiente, .env a medias, datos sin descargar y —lo mas importante— el
# arranque automatico, que es la razon de mover el sistema a un equipo 24/7.
#
# Uso:  ./tools/preparar_despliegue.sh

set -uo pipefail
cd "$(dirname "$0")/.."

VERDE=$'\033[32m'; ROJO=$'\033[31m'; AMAR=$'\033[33m'; GRIS=$'\033[90m'; FIN=$'\033[0m'
fallos=0; avisos=0

ok()    { echo "  ${VERDE}✔${FIN} $1"; }
mal()   { echo "  ${ROJO}✘${FIN} $1"; fallos=$((fallos+1)); }
aviso() { echo "  ${AMAR}!${FIN} $1"; avisos=$((avisos+1)); }
nota()  { echo "    ${GRIS}$1${FIN}"; }

echo "=============================================================="
echo "COMPROBACION PREVIA AL DESPLIEGUE"
echo "=============================================================="
echo
echo "Docker"
if command -v docker >/dev/null 2>&1; then
    ok "docker instalado ($(docker --version | cut -d, -f1))"
    if docker info >/dev/null 2>&1; then
        ok "el demonio responde"
    else
        mal "el demonio NO responde"
        nota "arranca Docker Desktop, o: sudo systemctl start docker"
    fi
    docker compose version >/dev/null 2>&1 && ok "docker compose disponible" \
        || mal "falta docker compose"
else
    mal "docker no esta instalado"
fi

echo
echo "Recursos"
if command -v free >/dev/null 2>&1; then
    RAM=$(free -m | awk '/^Mem:/{print $2}')
elif [[ "$(uname)" == "Darwin" ]]; then
    RAM=$(( $(sysctl -n hw.memsize) / 1024 / 1024 ))
else
    RAM=0
fi
if [[ "$RAM" -ge 4000 ]]; then
    ok "RAM total: ${RAM} MB (el sistema usa ~2.500 MB)"
elif [[ "$RAM" -gt 0 ]]; then
    mal "RAM total: ${RAM} MB — se necesitan al menos 4.000 MB"
else
    aviso "no se pudo medir la RAM"
fi

LIBRE=$(df -Pk . | awk 'NR==2{print int($4/1024/1024)}')
[[ "$LIBRE" -ge 20 ]] && ok "disco libre: ${LIBRE} GB" \
                      || aviso "disco libre: ${LIBRE} GB — se recomiendan 20 GB"

echo
echo "Configuracion"
if [[ -f .env ]]; then
    ok ".env existe"
    for var in FREQTRADE__API_SERVER__PASSWORD FREQTRADE__API_SERVER__JWT_SECRET_KEY; do
        valor=$(grep -E "^${var}=" .env | cut -d= -f2-)
        [[ -n "$valor" ]] && ok "$var configurada" || mal "$var vacia"
    done
    tg=$(grep -E "^TELEGRAM_TOKEN=" .env | cut -d= -f2-)
    [[ -n "$tg" ]] && ok "Telegram configurado" \
                   || aviso "sin Telegram: no habra notificaciones"
    ia=$(grep -E "^ANTHROPIC_API_KEY=" .env | cut -d= -f2-)
    [[ -n "$ia" ]] && ok "filtro de contexto con IA activo" \
                   || nota "sin ANTHROPIC_API_KEY: el filtro no bloqueara nada (falla abierto)"
    tge=$(grep -E "^FREQTRADE__TELEGRAM__ENABLED=" .env | cut -d= -f2-)
    [[ "$tge" == "false" ]] && ok "Telegram de los bots desactivado (lo lleva el vigilante)" \
        || mal "FREQTRADE__TELEGRAM__ENABLED debe ser false: cinco bots no pueden compartir un token"
    bk=$(grep -E "^BINANCE_API_KEY=" .env | cut -d= -f2-)
    [[ -z "$bk" ]] && ok "sin claves de Binance (correcto para dry-run)" \
                   || aviso "hay claves de Binance configuradas — revisa que sean de testnet"
else
    mal ".env no existe"
    nota "cp .env.example .env  y rellenalo"
fi

if grep -q '"dry_run": *true' user_data/config.dryrun.json 2>/dev/null; then
    ok "config en dry_run (dinero simulado)"
else
    mal "config.dryrun.json NO esta en dry_run"
fi

echo
echo "Datos"
n5m=$(ls user_data/data/*-5m.feather 2>/dev/null | wc -l | tr -d ' ')
n1h=$(ls user_data/data/*-1h.feather 2>/dev/null | wc -l | tr -d ' ')
[[ "$n5m" -ge 12 ]] && ok "velas de 5m: $n5m pares" \
    || mal "velas de 5m: $n5m pares — faltan. Corre ./tools/download_data.sh"
[[ "$n1h" -ge 12 ]] && ok "velas de 1h: $n1h pares" \
    || aviso "velas de 1h: $n1h pares"

echo
echo "Arranque automatico  ${GRIS}(la razon de mover el bot a esta maquina)${FIN}"
case "$(uname)" in
  Linux)
    if systemctl is-enabled docker >/dev/null 2>&1; then
        ok "docker arranca con el sistema"
    else
        mal "docker NO arranca solo — sudo systemctl enable docker"
    fi
    ;;
  Darwin)
    if osascript -e 'tell application "System Events" to get the name of every login item' 2>/dev/null | grep -qi docker; then
        ok "Docker Desktop arranca al iniciar sesion"
    else
        mal "Docker Desktop NO arranca solo"
        nota "Docker Desktop -> Settings -> General -> Start Docker Desktop when you sign in"
    fi
    dormir=$(pmset -g custom 2>/dev/null | awk '/^ *sleep/{print $2; exit}')
    if [[ "${dormir:-1}" == "0" ]]; then
        ok "el equipo no se suspende solo"
    else
        mal "el equipo se suspende a los ${dormir:-?} min — los bots se paran con el"
        nota "Ajustes del Sistema -> impedir que se duerma con la pantalla apagada"
        nota "apano temporal:  nohup caffeinate -is >/dev/null 2>&1 &"
    fi
    ;;
  *) aviso "sistema no reconocido: comprueba el arranque automatico a mano" ;;
esac

echo
echo "=============================================================="
if [[ "$fallos" -eq 0 && "$avisos" -eq 0 ]]; then
    echo "${VERDE}TODO LISTO${FIN} — arranca con:  docker compose up -d"
elif [[ "$fallos" -eq 0 ]]; then
    echo "${AMAR}LISTO CON $avisos AVISO(S)${FIN} — puedes arrancar, pero leelos."
    echo "docker compose up -d"
else
    echo "${ROJO}$fallos PROBLEMA(S)${FIN} y $avisos aviso(s). Resuelvelos antes de arrancar."
fi
echo "=============================================================="
exit $(( fallos > 0 ? 1 : 0 ))
