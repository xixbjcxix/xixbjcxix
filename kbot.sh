#!/usr/bin/env bash
# One-step launcher (macOS / Linux): creates a virtual env on first run, then runs kbot.
#   ./kbot.sh setup      ./kbot.sh check      ./kbot.sh demo      ./kbot.sh paper
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "First run: creating .venv and installing requirements..."
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python -m kbot "${@:-setup}"
