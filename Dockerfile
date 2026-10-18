# One container: FalkorDB (embedded server binary from falkordblite) + Tracewise,
# with the Flask graph built at image build time so the demo starts instantly.
# Debian trixie because the FalkorDB wheel needs glibc >= 2.39.
FROM python:3.13-slim-trixie

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY tracewise ./tracewise
RUN pip install --no-cache-dir ".[llm,lite]"

COPY scripts ./scripts
RUN chmod +x scripts/*.sh \
 && mkdir -p /data \
 && git clone --quiet https://github.com/pallets/flask /demo/flask \
 && python -m venv /demo/flask-venv \
 && /demo/flask-venv/bin/pip install --no-cache-dir --quiet -e /demo/flask pytest coverage asgiref python-dotenv \
 && BIN="$(python -c 'import redislite,os;print(os.path.dirname(redislite.__file__))')/bin" \
 && "$BIN/redis-server" --port 6379 --loadmodule "$BIN/falkordb.so" --dir /data --daemonize yes \
 && sleep 1 \
 && tracewise index /demo/flask --name flask --coverage-python /demo/flask-venv/bin/python \
 && "$BIN/redis-cli" -p 6379 SAVE \
 && "$BIN/redis-cli" -p 6379 SHUTDOWN NOSAVE || true

ENV PORT=8000 EMBEDDED_DB=1
EXPOSE 8000
CMD ["/app/scripts/entrypoint.sh"]
