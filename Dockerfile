FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    JGRANTS_HOST=0.0.0.0 \
    JGRANTS_PATH=/mcp \
    JGRANTS_REQUIRE_AUTH=1 \
    JGRANTS_FILES_DIR=/tmp/jgrants_files \
    PORT=8000

COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY jgrants_mcp_server ./jgrants_mcp_server

RUN uv sync --frozen --no-dev \
    && useradd --create-home --uid 10001 appuser \
    && mkdir -p /tmp/jgrants_files \
    && chown -R appuser:appuser /app /tmp/jgrants_files

EXPOSE 8000
USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD [".venv/bin/python", "-c", "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=3)"]

CMD [".venv/bin/python", "-m", "jgrants_mcp_server"]
