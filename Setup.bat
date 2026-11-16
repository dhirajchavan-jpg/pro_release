@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\Setup-Env.ps1" %*
exit /b %ERRORLEVEL%
