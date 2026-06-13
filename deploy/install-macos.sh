#!/bin/zsh
# Instalador del watchdog launchd para macOS.
#
# - Copia el plist a ~/Library/LaunchAgents/
# - Inyecta el TELEGRAM_BOT_TOKEN real (desde el entorno o ~/.zshrc) en la copia
#   instalada, sin que el secreto quede en git.
# - (Re)carga el agente.
#
# Uso:  ./deploy/install-macos.sh
set -e

LABEL="com.rnogales.deals-scraper"
PROJECT_DIR="${0:A:h}/.."
PROJECT_DIR="${PROJECT_DIR:A}"
SRC_PLIST="$PROJECT_DIR/deploy/$LABEL.plist"
DST_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"

# Obtener el token: del entorno o, si no, de ~/.zshrc
TOKEN="${TELEGRAM_BOT_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f "$HOME/.zshrc" ]; then
  TOKEN="$(grep -E '^export TELEGRAM_BOT_TOKEN=' "$HOME/.zshrc" | head -1 | sed -E 's/^export TELEGRAM_BOT_TOKEN=//; s/^"//; s/"$//')"
fi
if [ -z "$TOKEN" ]; then
  echo "ERROR: TELEGRAM_BOT_TOKEN no encontrado (ni en el entorno ni en ~/.zshrc)." >&2
  exit 1
fi

mkdir -p "$PROJECT_DIR/logs"

# Copiar plist e inyectar el token en la copia instalada (PlistBuddy maneja
# el valor de forma segura)
cp "$SRC_PLIST" "$DST_PLIST"
/usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:TELEGRAM_BOT_TOKEN $TOKEN" "$DST_PLIST"
plutil -lint "$DST_PLIST" >/dev/null

# (Re)cargar el agente
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$DST_PLIST"
launchctl enable "gui/$UID_NUM/$LABEL" 2>/dev/null || true

echo "Instalado y cargado: $LABEL"
launchctl list | grep "$LABEL" || true
