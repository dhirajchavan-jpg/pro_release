@echo off
setlocal
cd /d "%~dp0"

echo === Procurement Evaluation deployment ===
call Setup.bat
if errorlevel 1 exit /b %errorlevel%

call Restart_All.bat
if errorlevel 1 exit /b %errorlevel%

call Enable_Autostart.bat
if errorlevel 1 exit /b %errorlevel%

if /I "%PROCUREMENT_NO_LOG_WINDOW%"=="1" exit /b 0
start "Procurement Live Logs" cmd.exe /k call "%~dp0Logs.bat"
echo.
echo Deployment is ready. Live logs opened in a separate terminal.
exit /b 0
