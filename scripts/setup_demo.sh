#!/usr/bin/env bash
# Clone Flask, give it its own venv (for the coverage layer), and index it.
#   scripts/setup_demo.sh [workdir]
# FalkorDB must be reachable (tracewise db, or docker run -p 6379:6379 falkordb/falkordb).
set -euo pipefail
WORK="${1:-.demo}"
PY="${PYTHON:-python3}"
mkdir -p "$WORK"
if [ ! -d "$WORK/flask/.git" ]; then
  git clone --quiet https://github.com/pallets/flask "$WORK/flask"
fi
if [ ! -x "$WORK/flask-venv/bin/python" ]; then
  "$PY" -m venv "$WORK/flask-venv"
  "$WORK/flask-venv/bin/pip" install --quiet -e "$WORK/flask" pytest coverage asgiref python-dotenv
fi
tracewise index "$WORK/flask" --name flask --coverage-python "$WORK/flask-venv/bin/python"
