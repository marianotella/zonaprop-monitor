#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# setup_server.sh — Instala Zonaprop Monitor en un servidor Linux
#
# USO:
#   chmod +x setup_server.sh
#   ./setup_server.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

INSTALL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "🏠  Zonaprop Server Monitor — Setup"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# ─── Chequear config.json existente ───────────────────────────────────────────
SKIP_CONFIG=false
if [ -f "$INSTALL_DIR/config.json" ]; then
  echo "✅  Se encontró un config.json existente."
  echo -n "¿Usar la configuración actual? [S/n]: "
  read -r USE_EXISTING
  USE_EXISTING="${USE_EXISTING:-s}"
  if [[ "$USE_EXISTING" =~ ^[sS]$ ]]; then
    SKIP_CONFIG=true
    echo "   Usando config.json existente."
  else
    echo "   Se va a sobreescribir la configuración."
  fi
  echo ""
fi

if [ "$SKIP_CONFIG" = false ]; then

  # ─── SMTP ───────────────────────────────────────────────────────────────────
  echo "Configuración de email (para enviar notificaciones)"
  echo ""
  echo -n "SMTP host   [smtp.gmail.com]: "
  read -r SMTP_HOST
  SMTP_HOST="${SMTP_HOST:-smtp.gmail.com}"

  echo -n "SMTP port   [587]: "
  read -r SMTP_PORT
  SMTP_PORT="${SMTP_PORT:-587}"

  echo -n "Email (usuario SMTP): "
  read -r SMTP_USER

  echo -n "Contraseña / App Password: "
  read -r -s SMTP_PASS
  echo ""

  # ─── Búsquedas ──────────────────────────────────────────────────────────────
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "Búsquedas a monitorear"
  echo "(podés agregar más después editando config.json)"
  echo ""

  MONITORS_JSON=""
  INDEX=1

  while true; do
    echo "── Búsqueda #$INDEX ──────────────────────────────────"
    echo -n "Nombre (ej: Tella - Almagro): "
    read -r MON_NAME

    echo -n "URL de Zonaprop: "
    read -r MON_URL
    MON_URL="$(echo "$MON_URL" | tr -d '[:space:]')"

    echo -n "Email a notificar: "
    read -r MON_EMAIL

    ENTRY=$(printf '    {\n      "name": "%s",\n      "url": "%s",\n      "notify_email": "%s"\n    }' \
      "$MON_NAME" "$MON_URL" "$MON_EMAIL")

    if [ -n "$MONITORS_JSON" ]; then
      MONITORS_JSON="$MONITORS_JSON,\n$ENTRY"
    else
      MONITORS_JSON="$ENTRY"
    fi

    echo ""
    echo -n "¿Agregar otra búsqueda? [s/N]: "
    read -r MORE
    echo ""
    if [[ ! "$MORE" =~ ^[sS]$ ]]; then
      break
    fi
    INDEX=$((INDEX + 1))
  done

  # ─── Intervalo ────────────────────────────────────────────────────────────────
  echo -n "¿Cada cuántos minutos revisar? [60]: "
  read -r INTERVAL_MINS
  INTERVAL_MINS="$(echo "${INTERVAL_MINS:-60}" | tr -d '[:space:]')"
  INTERVAL_MINS="${INTERVAL_MINS:-60}"

  # ─── Confirmar ────────────────────────────────────────────────────────────────
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  SMTP:       $SMTP_USER @ $SMTP_HOST:$SMTP_PORT"
  echo "  Búsquedas:  $INDEX"
  echo "  Intervalo:  cada $INTERVAL_MINS minutos"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  echo -n "¿Confirmar? [s/N]: "
  read -r CONFIRM
  if [[ ! "$CONFIRM" =~ ^[sS]$ ]]; then
    echo "Instalación cancelada."
    exit 0
  fi

fi  # fin SKIP_CONFIG

echo ""
echo "📦  Instalando..."

# ─── Escribir config.json (solo si no se saltó) ───────────────────────────────
if [ "$SKIP_CONFIG" = false ]; then
cat > "$INSTALL_DIR/config.json" <<CONFIG
{
  "smtp": {
    "host": "$SMTP_HOST",
    "port": $SMTP_PORT,
    "username": "$SMTP_USER",
    "password": "$SMTP_PASS"
  },
  "monitors": [
$(echo -e "$MONITORS_JSON")
  ]
}
CONFIG
echo "✅  config.json generado"
fi  # fin escritura config

# ─── Entorno virtual ──────────────────────────────────────────────────────────
# Instalar python3-venv si no está disponible (Debian/Ubuntu)
if ! python3 -m venv --help &>/dev/null; then
  echo "📦  Instalando python3-venv..."
  if command -v apt-get &>/dev/null; then
    apt-get install -y python3-venv python3-pip
  elif command -v yum &>/dev/null; then
    yum install -y python3-venv python3-pip
  else
    echo "❌  No se pudo instalar python3-venv. Instalalo manualmente y volvé a correr el setup."
    exit 1
  fi
fi

if [ ! -d "$INSTALL_DIR/venv" ]; then
  python3 -m venv "$INSTALL_DIR/venv"
fi

# Verificar que el venv se creó bien
if [ ! -f "$INSTALL_DIR/venv/bin/pip" ]; then
  echo "❌  El entorno virtual no se creó correctamente."
  echo "    Probá: sudo apt-get install python3-venv python3-full"
  exit 1
fi

"$INSTALL_DIR/venv/bin/pip" install --upgrade pip --quiet
"$INSTALL_DIR/venv/bin/pip" install playwright playwright-stealth beautifulsoup4 --quiet
echo "✅  Dependencias Python instaladas"

echo "📦  Instalando Chromium (navegador headless)..."
"$INSTALL_DIR/venv/bin/playwright" install chromium --with-deps
echo "✅  Chromium instalado"

# ─── Cron job ─────────────────────────────────────────────────────────────────
# Si se saltó la config, leer intervalo del cron existente o usar 60 por defecto
if [ "$SKIP_CONFIG" = true ]; then
  EXISTING_CRON=$(crontab -l 2>/dev/null | grep "zonaprop.*monitor.py" || true)
  if [ -n "$EXISTING_CRON" ]; then
    INTERVAL_MINS=$(echo "$EXISTING_CRON" | grep -oP '^\*/\K[0-9]+' || echo "60")
    echo "   Manteniendo intervalo existente: cada $INTERVAL_MINS minutos"
  else
    INTERVAL_MINS=60
  fi
fi

CRON_CMD="*/$INTERVAL_MINS * * * * $INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/monitor.py >> $INSTALL_DIR/cron.log 2>&1"

# Evitar duplicados
( crontab -l 2>/dev/null | grep -v "zonaprop.*monitor.py"; echo "$CRON_CMD" ) | crontab -
echo "✅  Cron job instalado (cada $INTERVAL_MINS minutos)"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🏠  ¡Zonaprop Server Monitor instalado!"
echo ""
echo "  Config:     $INSTALL_DIR/config.json"
echo "  Logs:       $INSTALL_DIR/monitor.log"
echo "  Datos:      $INSTALL_DIR/data/"
echo ""
echo "  Probar ahora:"
echo "    $INSTALL_DIR/venv/bin/python3 $INSTALL_DIR/monitor.py"
echo ""
echo "  Ver cron:"
echo "    crontab -l"
echo ""
echo "  Agregar búsquedas: editá config.json y agregá entradas en 'monitors'"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
