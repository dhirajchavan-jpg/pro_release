@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\Request-Restart.ps1" %*
set "RC=%ERRORLEVEL%"
if "%RC%"=="0" exit /b 0
if not "%RC%"=="10" exit /b %RC%
echo Supervisor is not running; performing a direct restart.
call "%~dp0Stop_All.bat"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" exit /b %RC%
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\Start-All.ps1" %*
exit /b %ERRORLEVEL%
