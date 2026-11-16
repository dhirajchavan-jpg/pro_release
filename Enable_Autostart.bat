@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\Autostart.ps1"
exit /b %errorlevel%
