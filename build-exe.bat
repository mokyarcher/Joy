@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [Joy] .venv not found. Create it first.
  pause
  exit /b 1
)

echo [Joy] Installing PyInstaller...
".venv\Scripts\python.exe" -m pip install -q "pyinstaller>=6.0"
if errorlevel 1 (
  echo [Joy] pip install pyinstaller failed.
  pause
  exit /b 1
)

echo [Joy] Building single-file dist\Joy.exe ...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean joy.spec
if errorlevel 1 (
  echo [Joy] Build failed.
  pause
  exit /b 1
)

echo.
echo [Joy] Done: dist\Joy.exe
echo One file only. First launch may unpack for a few seconds.
echo.
pause
