@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap-node.ps1" %*
if errorlevel 1 (
  pause
  exit /b 1
)
