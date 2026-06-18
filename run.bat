@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creazione ambiente virtuale...
    python -m venv .venv
    if errorlevel 1 (
        echo ERRORE: Python non trovato. Installa Python 3.10+.
        pause
        exit /b 1
    )
)

echo Installazione dipendenze...
".venv\Scripts\pip" install -q -r requirements.txt
if errorlevel 1 (
    echo ERRORE: installazione dipendenze fallita.
    pause
    exit /b 1
)

".venv\Scripts\python" main.py
