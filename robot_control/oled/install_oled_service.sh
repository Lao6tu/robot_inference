#!/bin/bash
set -e

SERVICE_NAME="oled-display.service"
SERVICE_SRC="/home/drone/robot_control/oled/${SERVICE_NAME}"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"

echo "Installing OLED display service..."

if [[ ! -f "$SERVICE_SRC" ]]; then
    echo "Service file not found: $SERVICE_SRC"
    exit 1
fi

if [[ ! -x "/home/drone/robot_control/.venv/bin/python" ]]; then
    echo "Python executable not found: /home/drone/robot_control/.venv/bin/python"
    echo "Please create/install the virtual environment first."
    exit 1
fi

sudo cp "$SERVICE_SRC" "$SERVICE_DST"
sudo systemctl daemon-reload
sudo systemctl enable oled-display.service
sudo systemctl restart oled-display.service

echo ""
echo "Installation complete!"
echo ""
echo "Useful commands:"
echo "  sudo systemctl status oled-display        # Check status"
echo "  sudo systemctl stop oled-display          # Stop service"
echo "  sudo systemctl start oled-display         # Start service"
echo "  sudo systemctl restart oled-display       # Restart service"
echo "  sudo systemctl disable oled-display       # Disable autostart"
echo "  sudo journalctl -u oled-display -f        # View logs"
