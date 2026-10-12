#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_root"
export PATH="$project_root/.local/node_modules/.bin:$PATH"
gui_python=/usr/bin/python3
install_system_packages() {
  if [[ "$EUID" -eq 0 ]]; then "$@"; else sudo "$@"; fi
}
echo '[Bootstrap 1/6] Checking Node.js, npm, Python 3.11+, and Tk...'
command -v node >/dev/null || { echo 'Install Node.js first.'; exit 1; }
command -v npm >/dev/null || { echo 'npm is missing; reinstall Node.js.'; exit 1; }
command -v python3 >/dev/null || { echo 'Install Python 3.11+ first.'; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ is required"'
if [[ ! -x "$gui_python" ]]; then
  echo 'System Python is missing at /usr/bin/python3; install the system Python and Tk packages.'
  exit 1
fi
"$gui_python" -c 'import sys; assert sys.version_info >= (3, 11), "System Python 3.11+ is required for the GUI"'
if ! "$gui_python" -c 'import tkinter' 2>/dev/null; then
  echo 'Tk is missing. Installing the system Python Tk package...'
  if command -v apt-get >/dev/null; then install_system_packages apt-get update && install_system_packages apt-get install -y python3-tk
  elif command -v dnf >/dev/null; then install_system_packages dnf install -y python3-tkinter
  elif command -v pacman >/dev/null; then install_system_packages pacman -S --needed tk
  else echo 'Install Tk for /usr/bin/python3, then rerun this script.'; exit 1; fi
fi
echo '[Bootstrap 2/6] Checking dsh and pnpm...'
missing_packages=()
command -v dsh >/dev/null || missing_packages+=('@deepseek-ai/dsh')
command -v pnpm >/dev/null || missing_packages+=('pnpm')
if ((${#missing_packages[@]})); then npm install --prefix "$project_root/.local" "${missing_packages[@]}"; fi
echo '[Bootstrap 3/6] Checking Python virtual environment...'
if [[ ! -x .venv/bin/python ]]; then
  if ! python3 -m venv .venv; then
    if command -v apt-get >/dev/null; then
      echo 'Installing python3-venv...'
      install_system_packages apt-get install -y python3-venv
      python3 -m venv .venv
    else
      echo 'Python venv creation failed. Install the venv package for your distribution, then retry.'
      exit 1
    fi
  fi
fi
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  echo 'Repairing pip inside .venv...'
  .venv/bin/python -m ensurepip --upgrade
fi
echo '[Bootstrap 4/6] Installing Python dependencies...'
if ! .venv/bin/python -c 'import akshare, numpy, pandas, baostock, sklearn, xgboost' 2>/dev/null; then .venv/bin/python -m pip install -r requirements.txt; fi
echo '[Bootstrap 5/6] Checking local DSH plugin...'
if ! .venv/bin/python scripts/linux_plugin.py status; then .venv/bin/python scripts/linux_plugin.py register; fi
if [[ ! -f config/daily-push.json ]]; then cp config/daily-push.example.json config/daily-push.json; fi
echo '[Bootstrap 6/6] Starting control panel...'
exec "$gui_python" scripts/control_panel.py
