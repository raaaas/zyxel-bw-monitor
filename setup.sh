#!/usr/bin/env bash
# Set up Zyxel BW Monitor in a local virtual environment (Linux and macOS).
set -euo pipefail

cd "$(dirname "$0")"
VENV="${VENV:-.venv}"

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
	if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done

if [ -z "$PY" ]; then
	cat >&2 <<'MISSING'
python3 not found.
  Debian/Ubuntu : sudo apt install python3 python3-venv
  Fedora/RHEL   : sudo dnf install python3
  Arch          : sudo pacman -S python
  macOS         : brew install python
MISSING
	exit 1
fi

if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
	echo "$PY is Python $("$PY" -V 2>&1); this tool needs 3.9 or newer." >&2
	exit 1
fi

if ! "$PY" -c 'import ensurepip' >/dev/null 2>&1; then
	echo "The venv module is incomplete. On Debian/Ubuntu: sudo apt install python3-venv" >&2
	exit 1
fi

echo "Creating $VENV with $PY ($("$PY" -V 2>&1))"
mkdir -p "$VENV"
VENV="$(cd "$VENV" && pwd)"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r requirements.txt

cat <<DONE

Installed. To use it:

  source $VENV/bin/activate
  export ZX_PW='your ONT admin password'
  python read_usage.py --record usage_history.db
  python dashboard.py

Preview the dashboard with synthetic data (no device needed):

  python make_demo_data.py && python dashboard.py --db demo_history.db --out demo_dashboard.html
DONE
