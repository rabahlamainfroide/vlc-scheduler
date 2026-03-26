#!/usr/bin/env python3
"""
setup_autostart.py
------------------
Registers vlc_scheduler.py as a Windows Task Scheduler task that starts
automatically when the current user logs in.

Run this script ONCE (as Administrator for best results):
    python setup_autostart.py           # install
    python setup_autostart.py --remove  # uninstall
"""

import subprocess
import sys
from pathlib import Path

TASK_NAME   = "VLCScheduler"
SCRIPT      = Path(__file__).parent / "vlc_scheduler.py"
VENV_DIR    = Path(__file__).parent / ".venv"


def _find_python() -> Path:
    """
    Prefer the project's virtual-environment interpreter so the task runs
    with the correct dependencies installed.  Falls back to the interpreter
    that is running this script.
    """
    # Virtual environment (created by setup_venv.bat)
    for candidate in (
        VENV_DIR / "Scripts" / "pythonw.exe",   # venv, no console window
        VENV_DIR / "Scripts" / "python.exe",    # venv, with console fallback
    ):
        if candidate.exists():
            return candidate

    # Current interpreter (system or already-active venv)
    exe  = Path(sys.executable)
    winless = exe.parent / "pythonw.exe"
    return winless if winless.exists() else exe


PYTHONW_EXE = _find_python()


def create_task() -> None:
    print(f"Python  : {PYTHONW_EXE}")
    print(f"Script  : {SCRIPT}")
    print(f"Task    : {TASK_NAME}")
    print()

    # Build the XML-based task so we can set a 60-second startup delay
    # and run whether or not the user is logged on.
    task_xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>VLC video scheduler — plays videos at 13:00, 19:00 and 21:00</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <Delay>PT1M</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>3</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{PYTHONW_EXE}</Command>
      <Arguments>"{SCRIPT}"</Arguments>
      <WorkingDirectory>{SCRIPT.parent}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""

    xml_path = SCRIPT.parent / "_task_def.xml"
    xml_path.write_text(task_xml, encoding="utf-16")

    result = subprocess.run(
        ["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/XML", str(xml_path)],
        capture_output=True,
        text=True,
    )
    xml_path.unlink(missing_ok=True)   # clean up temp file

    if result.returncode == 0:
        print(f"Task '{TASK_NAME}' created successfully.")
        print("The VLC scheduler will start automatically at the next login.")
        print()
        print("To start it right now without rebooting:")
        print(f'    schtasks /Run /TN "{TASK_NAME}"')
    else:
        print("Failed to create the task:")
        print(result.stderr or result.stdout)
        print()
        print("Try running this script as Administrator:")
        print("  Right-click Command Prompt → 'Run as administrator'")
        print("  Then:  python setup_autostart.py")


def delete_task() -> None:
    result = subprocess.run(
        ["schtasks", "/Delete", "/F", "/TN", TASK_NAME],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print(f"Task '{TASK_NAME}' removed.")
    else:
        print("Could not remove task (it may not exist).")
        print(result.stderr or result.stdout)


if __name__ == "__main__":
    if "--remove" in sys.argv:
        delete_task()
    else:
        create_task()
