@echo off
setlocal
REM adaptbot launcher for Windows.
REM   Double-click for the menu, or:  start.bat login | check | paper | live | backtest
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if not exist "adaptbot\__main__.py" (
  echo.
  echo The bot files are not here. If you opened the zip by double-clicking it,
  echo close this window, right-click adaptbot.zip, choose "Extract All", then run
  echo login.bat from the extracted folder.
  echo.
  pause
  exit /b 1
)

if exist .venv\Scripts\python.exe goto :deps
echo First run: setting up Python for the bot (takes a minute)...
where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
if not exist .venv\Scripts\python.exe (
  echo.
  echo Python was not found. Install Python 3.10+ from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during install, then run this again.
  pause
  exit /b 1
)

:deps
.venv\Scripts\python -c "import aiohttp, cryptography, yaml" >nul 2>nul && goto :ready
echo Installing the bot's Python packages...
.venv\Scripts\python -m pip install -q --upgrade pip
.venv\Scripts\python -m pip install -q -r requirements.txt
if errorlevel 1 (
  echo.
  echo Installing packages failed - see the messages above.
  pause
  exit /b 1
)

:ready
if not "%~1"=="" (
  .venv\Scripts\python -m adaptbot %*
  echo.
  echo (finished - read any messages above^)
  pause
  exit /b
)
.venv\Scripts\python -m adaptbot
echo.
echo (finished - read any messages above^)
pause
