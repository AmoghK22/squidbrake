#!/usr/bin/env sh
# Connect an AI agent to a Squidbrake gateway:  ./connect.sh claude-code --project DIR
#   ./connect.sh wrap --sandbox --agent claude-code --url https://gateway.example.com --key gw_...
# The first run installs what it needs into .venv.
set -e
cd "$(dirname "$0")"

venv_python() {
  if [ -x .venv/bin/python ]; then echo .venv/bin/python; elif [ -x .venv/Scripts/python.exe ]; then echo .venv/Scripts/python.exe; fi
}

if [ -z "$(venv_python)" ]; then
  PY=$(command -v python3 || command -v python || true)
  if [ -z "$PY" ]; then
    echo "Python 3.10 or newer is needed: https://www.python.org/downloads/"
    exit 1
  fi
  echo "Setting up Squidbrake (first run only, takes a minute)..."
  if ! "$PY" -m venv .venv; then
    rm -rf .venv
    echo "Could not create a virtual environment. On Debian/Ubuntu: sudo apt install python3-venv"
    exit 1
  fi
fi
VPY=$(venv_python)

# Reinstall only when requirements.txt has changed since the last install.
if ! cmp -s requirements.txt .venv/installed.txt; then
  "$VPY" -m pip install --disable-pip-version-check -q -r requirements.txt
  cp requirements.txt .venv/installed.txt
fi

exec "$VPY" connect.py "$@"
