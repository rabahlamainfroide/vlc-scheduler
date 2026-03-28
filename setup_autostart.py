#!/usr/bin/env python3
"""
setup_autostart.py
------------------
Registers vlc_scheduler.py as a systemd user service that starts
automatically when the current user logs in.

Usage:
    python setup_autostart.py           # install
    python setup_autostart.py --remove  # uninstall
"""

import subprocess
import sys
from pathlib import Path

SERVICE_NAME = "vlc-scheduler"
SCRIPT       = Path(__file__).parent.resolve() / "vlc_scheduler.py"
VENV_DIR     = Path(__file__).parent.resolve() / ".venv"
SYSTEMD_DIR  = Path.home() / ".config" / "systemd" / "user"
SERVICE_FILE = SYSTEMD_DIR / f"{SERVICE_NAME}.service"


def _find_python() -> Path:
    """
    Prefer the project's virtual-environment interpreter so the service runs
    with the correct dependencies installed.  Falls back to the interpreter
    that is running this script.
    """
    candidate = VENV_DIR / "bin" / "python"
    if candidate.exists():
        return candidate
    return Path(sys.executable)


def create_service() -> None:
    python_exe = _find_python()

    print(f"Python  : {python_exe}")
    print(f"Script  : {SCRIPT}")
    print(f"Service : {SERVICE_NAME}.service")
    print()

    service_unit = f"""[Unit]
Description=VLC Scheduler — plays videos at scheduled times
After=graphical-session.target

[Service]
Type=simple
ExecStart={python_exe} {SCRIPT}
WorkingDirectory={SCRIPT.parent}
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
"""

    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    SERVICE_FILE.write_text(service_unit)

    result = subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("Failed to reload systemd daemon:")
        print(result.stderr or result.stdout)
        sys.exit(1)

    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", SERVICE_NAME],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"Service '{SERVICE_NAME}' created and started successfully.")
        print("The VLC scheduler will start automatically at the next login.")
        print()
        print("Useful commands:")
        print(f"    systemctl --user status {SERVICE_NAME}")
        print(f"    systemctl --user stop   {SERVICE_NAME}")
        print(f"    journalctl --user -u {SERVICE_NAME} -f")
    else:
        print("Failed to enable the service:")
        print(result.stderr or result.stdout)


def delete_service() -> None:
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", SERVICE_NAME],
        capture_output=True, text=True,
    )
    if SERVICE_FILE.exists():
        SERVICE_FILE.unlink()
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"],
        capture_output=True, text=True,
    )
    print(f"Service '{SERVICE_NAME}' removed.")


if __name__ == "__main__":
    if "--remove" in sys.argv:
        delete_service()
    else:
        create_service()
