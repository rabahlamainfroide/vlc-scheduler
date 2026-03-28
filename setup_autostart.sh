#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [ ! -f "$VENV_PYTHON" ]; then
    echo "ERROR: Virtual environment not found."
    echo "Please run setup_venv.sh first."
    exit 1
fi

echo "Running setup_autostart.py with the virtual environment Python..."
echo
"$VENV_PYTHON" "$SCRIPT_DIR/setup_autostart.py" "$@"
