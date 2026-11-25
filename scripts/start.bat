@echo off
rem Meiya Inbound Assistant V1.0 - Windows launcher
rem Double-click to start the local web UI and open the browser.
setlocal
cd /d "%~dp0.."

set "PY=python"
if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"

set "PORT=8799"

echo Starting Meiya Inbound Assistant V1.0 ...
"%PY%" "tools\ui.py" --port %PORT% --open
if errorlevel 1 (
  echo.
  echo Failed to start. Install dependencies first:
  echo     python -m pip install -r requirements.txt
  pause
)
endlocal
