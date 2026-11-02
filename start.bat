@echo off
rem adaptbot launcher for Windows
cd /d "%~dp0"
if not exist .venv (
  echo First run: creating Python environment...
  python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
  .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python -m adaptbot %*
pause
