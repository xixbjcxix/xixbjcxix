#!/usr/bin/env bash
# One-step launcher (macOS / Linux): creates a virtual env on first run, then runs pbot.
#   ./pbot.sh setup      ./pbot.sh check      ./pbot.sh sim      ./pbot.sh paper      ./pbot.sh live
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "First run: creating .venv and installing requirements..."
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m pbot "${@:-sim}"
