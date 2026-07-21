FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.30 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY timelapse ./timelapse
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TIMELAPSE_WEB_DATA_DIR=/data \
    TIMELAPSE_WEB_OUTPUT_DIR=/data/exports \
    TIMELAPSE_WEB_HOST=0.0.0.0 \
    TIMELAPSE_WEB_PORT=8000

RUN useradd --create-home --uid 10001 timelapse \
    && mkdir -p /data/exports \
    && chown -R timelapse:timelapse /data

COPY --from=builder /build/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

USER timelapse
WORKDIR /app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["python", "-m", "timelapse.web"]
