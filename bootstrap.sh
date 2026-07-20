#!/usr/bin/env bash
# Bootstrap the rekordvibes skill: create the venv, install core
# deps, run the environment diagnostic. Idempotent — safe to re-run.
set -euo pipefail
cd "$(dirname "$0")"

# Pick an interpreter explicitly — bare `python3` on a fresh Mac can be the
# 3.9 Xcode CLT stub, which is too old.
PY=""
for cand in python3.13 python3.12 python3.11; do
  if command -v "$cand" >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -z "$PY" ] && command -v python3 >/dev/null 2>&1 \
   && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
  PY=python3
fi
if [ -z "$PY" ]; then
  echo "ERROR: no Python 3.11+ found." >&2
  echo "Looked for: python3.13, python3.12, python3.11, python3 (>=3.11)." >&2
  echo "Install one, e.g.: brew install python@3.12" >&2
  exit 1
fi
echo "Using $("$PY" -V) at $(command -v "$PY")"

[ -d venv ] || "$PY" -m venv venv
./venv/bin/pip install --quiet --upgrade pip
./venv/bin/pip install --quiet -r scripts/requirements.txt

echo
echo "Core install done. Optional analysis extras (~400 MB; rhythm/timbre lanes):"
echo "  ./venv/bin/pip install -r scripts/requirements-analysis.txt"
echo
./venv/bin/python scripts/rbx.py setup
