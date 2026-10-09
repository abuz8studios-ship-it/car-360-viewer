@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-LotVue360.ps1" %*
if errorlevel 1 (
  echo.
  echo LotVue360 installation failed. Read the error above and try again.
  pause
)
endlocal
