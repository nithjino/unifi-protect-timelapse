FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91 AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.30@sha256:93b61e21202b1dab861092748e46bbd6e0e41dd84f59b9174efd2353186e1b47 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY timelapse ./timelapse
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

ARG TIMELAPSE_UID=1000
ARG TIMELAPSE_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TIMELAPSE_WEB_DATA_DIR=/data \
    TIMELAPSE_WEB_OUTPUT_DIR=/data/exports \
    TIMELAPSE_WEB_HOST=0.0.0.0 \
    TIMELAPSE_WEB_PORT=8000

RUN apt-get update \
    && apt-get install --yes --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && if ! getent group "${TIMELAPSE_GID}" >/dev/null; then groupadd --gid "${TIMELAPSE_GID}" timelapse; fi \
    && useradd --create-home --uid "${TIMELAPSE_UID}" --gid "${TIMELAPSE_GID}" timelapse \
    && mkdir -p /data/exports \
    && chown -R "${TIMELAPSE_UID}:${TIMELAPSE_GID}" /data

COPY --from=builder /build/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

USER timelapse
WORKDIR /app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

CMD ["python", "-m", "timelapse.web"]
