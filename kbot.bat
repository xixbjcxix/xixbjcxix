@echo off
setlocal
REM Kalshi bot launcher for Windows.
REM   Double-click it for a menu, or from Command Prompt:  kbot setup | kbot check | kbot demo | kbot paper
REM   From PowerShell type:  .\kbot demo
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if exist .venv\Scripts\python.exe goto :ready
echo First run: setting up (this takes a minute)...
where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
if not exist .venv\Scripts\python.exe (
  echo.
  echo Python was not found. Install Python 3.10+ from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during install, then run this again.
  pause
  exit /b 1
)
.venv\Scripts\python -m pip install -q --upgrade pip
.venv\Scripts\python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo Installing requirements failed - see the messages above.
  pause
  exit /b 1
)

:ready
if not "%~1"=="" (
  .venv\Scripts\python -m kbot %*
  exit /b %errorlevel%
)

:menu
echo.
echo  Kalshi bot
echo  ----------
echo   1  Setup (enter API keys)
echo   2  Check keys and markets
echo   3  Start DEMO trading + dashboard
echo   4  Start PAPER trading (simulated fills) + dashboard
echo   5  Record market data
echo   6  Start LIVE trading - REAL MONEY on kalshi.com
echo   7  Quit
echo.
set /p choice="Choose 1-7: "
if "%choice%"=="1" .venv\Scripts\python -m kbot setup
if "%choice%"=="2" .venv\Scripts\python -m kbot check
if "%choice%"=="3" .venv\Scripts\python -m kbot demo
if "%choice%"=="4" .venv\Scripts\python -m kbot paper
if "%choice%"=="5" .venv\Scripts\python -m kbot record
if "%choice%"=="6" .venv\Scripts\python -m kbot live --i-understand-real-money
if "%choice%"=="7" exit /b 0
echo.
echo (finished - read any messages above)
pause
goto :menu
