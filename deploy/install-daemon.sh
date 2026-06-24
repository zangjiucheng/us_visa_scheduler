#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_USER="${SERVICE_USER:-$USER}"
SERVICE_NAME="${SERVICE_NAME:-visa-scheduler}"

if [[ ! -f "$INSTALL_DIR/config.ini" ]]; then
  echo "Missing $INSTALL_DIR/config.ini — copy config.ini.example and edit it first."
  exit 1
fi

if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
  echo "Creating virtualenv in $INSTALL_DIR/.venv"
  python3 -m venv "$INSTALL_DIR/.venv"
fi

echo "Installing Python dependencies..."
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

mkdir -p "$INSTALL_DIR/logs"

if ! command -v google-chrome >/dev/null 2>&1 && ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1; then
  echo "Warning: Chrome/Chromium not found in PATH. Install it before starting the daemon."
fi

SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
TMP_SERVICE="$(mktemp)"
sed \
  -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
  -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
  "$INSTALL_DIR/deploy/visa-scheduler.service" > "$TMP_SERVICE"

echo "Installing systemd unit to $SERVICE_FILE"
sudo cp "$TMP_SERVICE" "$SERVICE_FILE"
rm -f "$TMP_SERVICE"

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

echo
echo "Daemon installed."
echo "  Start:   sudo systemctl start $SERVICE_NAME"
echo "  Status:  sudo systemctl status $SERVICE_NAME"
echo "  Logs:    tail -f $INSTALL_DIR/logs/daemon.log"
echo "  Journal: sudo journalctl -u $SERVICE_NAME -f"
echo
echo "Set HEADLESS = True in config.ini when running on a headless server."
