@echo off
chcp 65001 >nul
setlocal

set "ROOT=%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH.
  echo Run this script with an installed Python, or execute:
  echo   E:\TTS\GPT-SoVITS-v2pro-20250604\runtime\python.exe "%ROOT%tools\check_environment.py"
  echo.
  pause
  exit /b 1
)

python "%ROOT%tools\check_environment.py" %*
echo.
pause
