#!/usr/bin/env bash
# Container entrypoint.
#  - EMBEDDED_DB=1 (default): start the bundled FalkorDB on :6379 with the graph baked in at build time.
#  - EMBEDDED_DB=0: use FALKORDB_HOST / FALKORDB_URL (docker-compose, FalkorDB Cloud) and index on first boot.
set -euo pipefail
if [ "${EMBEDDED_DB:-1}" = "1" ]; then
  BIN="$(python -c 'import redislite,os;print(os.path.dirname(redislite.__file__))')/bin"
  "$BIN/redis-server" --port 6379 --loadmodule "$BIN/falkordb.so" --dir /data --save 300 1 \
    --protected-mode no --daemonize yes --logfile /data/falkordb.log
fi
# Wait for FalkorDB (embedded or external) to accept connections.
for _ in $(seq 1 150); do tracewise repos >/dev/null 2>&1 && break; sleep 0.2; done
if ! tracewise repos | grep -qx flask; then
  echo "indexing flask (first boot)..."
  tracewise index /demo/flask --name flask --coverage-python /demo/flask-venv/bin/python
fi
exec tracewise serve --host 0.0.0.0 --port "${PORT:-8000}"
