@echo off
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\Show-Cost.ps1" %*
exit /b %ERRORLEVEL%
