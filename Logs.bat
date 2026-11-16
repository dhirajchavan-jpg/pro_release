@echo off
setlocal
call "%~dp0Cost.bat"
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\windows\Watch-Logs.ps1"
exit /b %errorlevel%
