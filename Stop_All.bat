@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\Stop-All.ps1" %*
exit /b %ERRORLEVEL%
