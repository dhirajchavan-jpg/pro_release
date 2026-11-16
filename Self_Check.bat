@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\Self-Check.ps1"
exit /b %errorlevel%
