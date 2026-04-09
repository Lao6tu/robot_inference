#!/bin/bash
set -e

SERVICE_NAME="oled-display.service"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SERVICE_SRC="${SCRIPT_DIR}/${SERVICE_NAME}"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"
PYTHON_BIN="${PROJECT_ROOT}/.venv/bin/python"
TMP_SERVICE_FILE="$(mktemp)"

echo "Installing OLED display service..."

if [[ ! -f "$SERVICE_SRC" ]]; then
    echo "Service file not found: $SERVICE_SRC"
    exit 1
fi

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "Python executable not found: $PYTHON_BIN"
    echo "Please create/install the virtual environment first."
    exit 1
fi

sed "s|{{PROJECT_ROOT}}|${PROJECT_ROOT}|g" "$SERVICE_SRC" > "$TMP_SERVICE_FILE"
sudo cp "$TMP_SERVICE_FILE" "$SERVICE_DST"
rm -f "$TMP_SERVICE_FILE"
sudo systemctl daemon-reload
sudo systemctl enable oled-display.service
sudo systemctl restart oled-display.service

echo ""
echo "Installation complete!"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status oled-display.service     # Check status"
echo "  sudo systemctl stop oled-display.service       # Stop service"
echo "  sudo systemctl start oled-display.service      # Start service"
echo "  sudo systemctl restart oled-display.service    # Restart service"
echo "  sudo systemctl disable oled-display.service    # Disable autostart"
echo "  sudo journalctl -u oled-display.service -f     # View logs"
