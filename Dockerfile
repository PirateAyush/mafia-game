# syntax=docker/dockerfile:1

# ---------- Stage 1: build the app's virtual environment ----------
FROM python:3.12-slim AS builder

# Copy the uv binary from its official image, pinned to the version you use locally.
COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 1) Dependencies only. This layer is reused until pyproject.toml or uv.lock changes,
#    so editing your code doesn't re-download every package.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# 2) The app itself, installed as a normal (non-editable) package.
COPY README.md ./
COPY src ./src
RUN uv sync --locked --no-dev --no-editable


# ---------- Stage 2: the small image that actually runs ----------
FROM python:3.12-slim

# Run as an ordinary user, never root.
RUN useradd --create-home app
WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
USER app

EXPOSE 8000

# Docker marks the container unhealthy if /healthz stops answering.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]

CMD ["uvicorn", "mafia_game.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--ws-max-size", "65536"]
