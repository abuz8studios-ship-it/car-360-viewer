@echo off
rem Double-click to build LotVue360-OneClick-windows-x64.exe on this PC.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Build-Windows-Installer.ps1"
echo.
pause
