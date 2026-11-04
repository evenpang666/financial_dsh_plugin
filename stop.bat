@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
node scripts\stop.mjs %*
set "stop_result=%errorlevel%"
echo.
pause
exit /b %stop_result%
