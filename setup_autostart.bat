@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "VENV_PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"

if not exist "%VENV_PYTHON%" (
    echo ERROR: Virtual environment not found.
    echo Please run setup_venv.bat first.
    pause
    exit /b 1
)

echo Running setup_autostart.py with the virtual environment Python...
echo (You may be prompted by UAC for administrator rights.)
echo.

"%VENV_PYTHON%" "%SCRIPT_DIR%setup_autostart.py" %*

pause
