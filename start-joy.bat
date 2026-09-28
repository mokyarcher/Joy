@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [Joy] .venv not found.
  echo Run: python -m venv .venv
  echo Then: .venv\Scripts\pip install -e ".[ui]"
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m joy
if errorlevel 1 pause