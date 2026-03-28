@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "VENV_DIR=%SCRIPT_DIR%.venv"

echo ============================================================
echo  VLC Scheduler — virtual environment setup
echo ============================================================
echo.

REM Create venv if it doesn't exist yet
if not exist "%VENV_DIR%\Scripts\python.exe" (
    echo Creating virtual environment in .venv ...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo ERROR: Could not create virtual environment.
        echo Make sure Python 3 is installed and on your PATH.
        pause
        exit /b 1
    )
    echo Done.
) else (
    echo Virtual environment already exists — skipping creation.
)

echo.
echo Installing / updating dependencies ...
"%VENV_DIR%\Scripts\pip" install --upgrade pip --quiet
"%VENV_DIR%\Scripts\pip" install -r "%SCRIPT_DIR%requirements.txt"
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  Dependencies installed successfully.
echo.
echo  Next step — register the Windows scheduled task:
echo    setup_autostart.bat
echo  or run manually:
echo    .venv\Scripts\python vlc_scheduler.py
echo ============================================================
echo.
pause
