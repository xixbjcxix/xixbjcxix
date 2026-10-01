@echo off
setlocal
REM Public.com day-trading bot launcher for Windows.
REM   Double-click it for a menu, or from Command Prompt:  pbot setup | pbot check | pbot sim | pbot paper
REM   From PowerShell type:  .\pbot paper
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if exist .venv\Scripts\python.exe goto :ready
if not "%~1"=="" (
  .venv\Scripts\python -m pbot %*
  exit /b %errorlevel%
)

:menu
echo.
echo  Public day-trading bot
echo  ----------------------
echo   1  Setup (enter API secret key)
echo   2  Check key, account and quotes
echo   3  Offline SIM demo (synthetic market)
echo   4  Start PAPER trading (real quotes, simulated fills)
echo   5  Today's report
echo   6  Start LIVE trading - REAL MONEY on Public
echo   7  Quit
echo.
set /p choice="Choose 1-7: "
if "%choice%"=="1" .venv\Scripts\python -m pbot setup
if "%choice%"=="2" .venv\Scripts\python -m pbot check
if "%choice%"=="3" .venv\Scripts\python -m pbot sim --days 5
if "%choice%"=="4" .venv\Scripts\python -m pbot paper
if "%choice%"=="5" .venv\Scripts\python -m pbot report
if "%choice%"=="6" .venv\Scripts\python -m pbot live
if "%choice%"=="7" exit /b 0
echo.
echo (finished - read any messages above)
pause
goto :menu
