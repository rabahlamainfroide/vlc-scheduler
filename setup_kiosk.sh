#!/usr/bin/env bash
# setup_kiosk.sh
# Configures a minimal Debian machine as a VLC kiosk:
#   - Auto-login on tty1
#   - Auto-start X on login
#   - Run vlc_scheduler on X startup, with auto-restart on crash
#
# Usage: sudo bash setup_kiosk.sh [username]
#   username defaults to $SUDO_USER if not provided

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Require root ───────────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: Run this script as root:  sudo bash setup_kiosk.sh"
    exit 1
fi

# ── Determine target username ──────────────────────────────────────────────────
USERNAME="${1:-${SUDO_USER:-}}"
if [ -z "$USERNAME" ]; then
    echo "ERROR: Could not detect username. Run as:  sudo bash setup_kiosk.sh <username>"
    exit 1
fi

USER_HOME="$(getent passwd "$USERNAME" | cut -d: -f6)"

echo "============================================================"
echo " VLC Scheduler — Kiosk Setup"
echo "============================================================"
echo " User    : $USERNAME"
echo " Home    : $USER_HOME"
echo " Project : $SCRIPT_DIR"
echo

# ── 1. Install system packages ─────────────────────────────────────────────────
echo "[1/4] Installing system packages ..."
apt-get update -q
apt-get install -y xorg vlc python3 python3-schedule
echo "      Done."
echo

# ── 2. Configure auto-login on tty1 ───────────────────────────────────────────
echo "[2/4] Configuring auto-login for '$USERNAME' on tty1 ..."
GETTY_DIR="/etc/systemd/system/getty@tty1.service.d"
mkdir -p "$GETTY_DIR"
cat > "$GETTY_DIR/autologin.conf" << EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $USERNAME --noclear %I \$TERM
EOF
systemctl daemon-reload
echo "      Done."
echo

# ── 3. Configure auto-start X on tty1 login ───────────────────────────────────
echo "[3/4] Configuring auto-start X ..."
BASH_PROFILE="$USER_HOME/.bash_profile"

if ! grep -q "vlc-scheduler-kiosk" "$BASH_PROFILE" 2>/dev/null; then
    cat >> "$BASH_PROFILE" << 'PROFILE'

# vlc-scheduler-kiosk: start X automatically when logged in on tty1
if [ -z "${DISPLAY:-}" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec startx
fi
PROFILE
    chown "$USERNAME:$USERNAME" "$BASH_PROFILE"
    echo "      Added startx block to ~/.bash_profile"
else
    echo "      ~/.bash_profile already configured — skipped."
fi
echo

# ── 4. Create .xinitrc to run the scheduler ────────────────────────────────────
echo "[4/4] Creating ~/.xinitrc ..."
cat > "$USER_HOME/.xinitrc" << XINITRC
#!/bin/bash
# Disable screen blanking and power management
xset s off
xset -dpms
xset s noblank

# Run VLC Scheduler — restart automatically if it ever crashes
cd "$SCRIPT_DIR"
while true; do
    python3 vlc_scheduler.py
    sleep 5
done
XINITRC
chmod +x "$USER_HOME/.xinitrc"
chown "$USERNAME:$USERNAME" "$USER_HOME/.xinitrc"
echo "      Done."
echo

# ── Done ───────────────────────────────────────────────────────────────────────
echo "============================================================"
echo " Setup complete!"
echo "============================================================"
echo
echo " Next steps:"
echo "   1. Edit config.json with your video folders and schedule:"
echo "      nano $SCRIPT_DIR/config.json"
echo
echo "   2. Reboot to start the kiosk:"
echo "      sudo reboot"
echo
echo " Useful commands while running:"
echo "   Status : curl http://127.0.0.1:8765/"
echo "   Log    : tail -f $SCRIPT_DIR/vlc_scheduler.log"
echo
