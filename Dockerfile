FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HIVE_DB_PATH=/data/agents.db \
    HIVE_WORKSPACE=/data/workspace \
    HIVE_HOST=0.0.0.0 \
    HIVE_PORT=8000

WORKDIR /app

# Create a non-root user and persistent data/workspace directories
RUN adduser --disabled-password --gecos "" --uid 1000 hive \
    && mkdir -p /data/workspace \
    && chown -R hive:hive /app /data

COPY --chown=hive:hive main.py config.toml.example ./
COPY --chown=hive:hive hive ./hive

USER hive

VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"HIVE_PORT\", \"8000\")}/health', timeout=3)"

# Default: HTTP API server for deployments; override with `python main.py` for interactive CLI
CMD ["python", "main.py", "--serve"]
