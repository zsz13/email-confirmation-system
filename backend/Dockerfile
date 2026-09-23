# syntax=docker/dockerfile:1

# ---- Build stage: resolve the locked dependencies into a virtual environment ----
FROM python:3.14-slim-trixie AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Only the lockfile inputs first, so dependency resolution is cached across source edits.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- Runtime stage: the interpreter, the resolved environment and the application code ----
FROM python:3.14-slim-trixie AS runtime

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app

WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app app ./app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

EXPOSE 8000

# The slim image has no curl; the interpreter is already there.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2).status == 200 else 1)"]

# --proxy-headers honours X-Forwarded-For, but only from peers listed in FORWARDED_ALLOW_IPS
# (Uvicorn's default is 127.0.0.1, i.e. nobody in a container). The deployment that puts a
# reverse proxy in front of this image sets that variable; see compose.yaml.
# --no-access-log: the application logs every request itself (app/core/logging.py) with the
# request id, the status and the duration, and keeps the health check the container polls
# every ten seconds at DEBUG. Uvicorn's own access line would duplicate it and bury the rest.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-access-log"]
