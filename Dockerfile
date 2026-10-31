# Dockerfile
# Multi-stage Dockerfile for Rogue App Discovery Service
# Two services: api and worker (share the same image, different CMD)

FROM python:3.12-slim AS base

WORKDIR /app

# System deps for lxml, chardet, hiredis
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt1-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

# Source is copied in each target, after the slow dependency layers, so code changes
# do not reinstall Chromium.

# ── API target ─────────────────────────────────────────────────────────────────
FROM base AS api
ENV WEB_CONCURRENCY=1
COPY . .
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]

# ── Worker target ──────────────────────────────────────────────────────────────
FROM base AS worker
RUN pip install --no-cache-dir -e ".[worker]"
# Playwright + Chromium for sources that require JS rendering (e.g. uptodown)
RUN pip install --no-cache-dir playwright && playwright install --with-deps chromium
COPY . .
CMD ["dramatiq", "app.jobs.actors", "--processes", "1", "--threads", "4", "--queues", "scans", "webhooks", "discovery"]

# Note: Docker image was written but NOT built and run in this development session.
# The compose file above defines the two-service setup with DATA_DIR on a mounted volume.
