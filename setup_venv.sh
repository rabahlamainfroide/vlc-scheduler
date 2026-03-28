#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

echo "============================================================"
echo " VLC Scheduler — virtual environment setup"
echo "============================================================"
echo

if [ ! -f "$VENV_DIR/bin/python" ]; then
    echo "Creating virtual environment in .venv ..."
    python3 -m venv "$VENV_DIR"
    echo "Done."
else
    echo "Virtual environment already exists — skipping creation."
fi

echo
echo "Installing / updating dependencies ..."
"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"

echo
echo "============================================================"
echo " Dependencies installed successfully."
echo
echo " Next step — register the systemd service:"
echo "   python setup_autostart.py"
echo " or run manually:"
echo "   .venv/bin/python vlc_scheduler.py"
echo "============================================================"
echo
