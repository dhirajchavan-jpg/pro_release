@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\Start-All.ps1" %*
exit /b %ERRORLEVEL%
