@echo off
setlocal

cd /d "%~dp0"

set "PY_CMD="
set "PY_ARGS="
set "VENV_DIR=%~dp0.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"

if exist "%VENV_PYTHON%" (
    set "PY_CMD=%VENV_PYTHON%"
)

if not defined PY_CMD (
    where py >nul 2>nul
    if not errorlevel 1 (
        set "PY_CMD=py"
        set "PY_ARGS=-3"
    )
)

if not defined PY_CMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PY_CMD=python"
)

if not defined PY_CMD (
    where python3 >nul 2>nul
    if not errorlevel 1 set "PY_CMD=python3"
)

if not defined PY_CMD (
    echo [FEHLER] Python wurde nicht gefunden.
    echo Bitte Python 3.10+ installieren und beim Setup zur PATH-Variable hinzufuegen.
    pause
    exit /b 1
)

if not exist "%VENV_PYTHON%" (
    echo [INFO] Erstelle lokale virtuelle Umgebung in .venv ...
    call "%PY_CMD%" %PY_ARGS% -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [FEHLER] Die virtuelle Umgebung konnte nicht erstellt werden.
        pause
        exit /b 1
    )
)

echo [INFO] Aktualisiere pip ...
call "%VENV_PYTHON%" -m pip install --upgrade pip
if errorlevel 1 (
    echo [FEHLER] pip konnte nicht aktualisiert werden.
    pause
    exit /b 1
)

echo [INFO] Installiere/aktualisiere Abhaengigkeiten in .venv ...
call "%VENV_PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [FEHLER] Abhaengigkeiten konnten nicht installiert werden.
    pause
    exit /b 1
)

if not defined PORT set "PORT=5000"
if not defined HOST set "HOST=0.0.0.0"

echo [INFO] Starte YouTube Downloader auf http://127.0.0.1:%PORT%
start "" "http://127.0.0.1:%PORT%"
call "%VENV_PYTHON%" app.py

endlocal
