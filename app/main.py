# app/main.py
"""
FastAPI application entry point.

Single-worker enforcement: if WEB_CONCURRENCY > 1 or --workers > 1 is
detected, log a fatal error and exit immediately to prevent state corruption.

A file lock (DATA_DIR/.api.lock) prevents two API instances from running
simultaneously against the same data directory.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from typing import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from filelock import FileLock, Timeout

from app.api.routes.brand_details import router as brand_details_router
from app.api.routes.scans import router as scans_router
from app.config.settings import get_settings
from app.utils.http_client import close_client

log = logging.getLogger(__name__)

# App loggers (adapters, orchestrator, stores) log through the root logger; uvicorn only configures its own.
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s")

_worker_proc: subprocess.Popen | None = None


def _enforce_single_worker() -> None:
    """Die loudly if more than one uvicorn worker is configured."""
    web_concurrency = int(os.environ.get("WEB_CONCURRENCY", "1"))
    if web_concurrency > 1:
        log.critical(
            "WEB_CONCURRENCY=%d detected — this service must run with exactly 1 worker. "
            "Multiple workers race on Redis state. Exiting.",
            web_concurrency,
        )
        sys.exit(1)


_DEFAULT_SECRET = "change-me-in-production"


def _enforce_secrets() -> None:
    """Refuse to start with default secrets unless ALLOW_INSECURE_DEFAULTS=true."""
    settings = get_settings()
    insecure = [
        name for name, value in (
            ("API_KEY", settings.api_key),
        ) if value == _DEFAULT_SECRET
    ]
    if not insecure:
        return
    if settings.allow_insecure_defaults:
        log.warning("Default secrets in use (%s) — auth/signing ineffective.", ", ".join(insecure))
        return
    log.critical(
        "Default value for %s — set real secrets, or ALLOW_INSECURE_DEFAULTS=true for local dev. Exiting.",
        ", ".join(insecure),
    )
    sys.exit(1)


_REAP_SECONDS = 30


async def _reap_loop(redis: aioredis.Redis) -> None:
    """Every _REAP_SECONDS: free the slots of scans whose worker died, and start the waiting scans."""
    from app.jobs.actors import promote_pending

    while True:
        try:
            await promote_pending(redis)
        except Exception:  # a failed pass must not stop the next one
            log.exception("slot reaper pass failed")
        await asyncio.sleep(_REAP_SECONDS)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    _enforce_single_worker()
    _enforce_secrets()

    # Ensure data directories exist
    settings.results_dir().mkdir(parents=True, exist_ok=True)
    settings.tmp_dir().mkdir(parents=True, exist_ok=True)
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)

    # File lock — prevents two API instances
    lock_path = settings.api_lock_path()
    lock = FileLock(str(lock_path), timeout=1)
    try:
        lock.acquire()
    except Timeout:
        log.critical(
            "Could not acquire lock at %s — another API instance may be running. Exiting.",
            lock_path,
        )
        sys.exit(1)

    # Redis connection pool
    pool = aioredis.ConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        decode_responses=False,
    )
    app.state.redis = aioredis.Redis(connection_pool=pool)

    # Embedded Dramatiq worker (killed automatically on shutdown). Skipped when a worker container consumes the queues.
    global _worker_proc
    if settings.embedded_worker:
        _worker_proc = subprocess.Popen(
            [
                sys.executable, "-m", "dramatiq",
                "app.jobs.actors",
                "--processes", "1",
                "--threads", str(settings.max_concurrent_scans),
                "--queues", "scans", "webhooks",
            ],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        log.info("Dramatiq worker started (PID %d)", _worker_proc.pid)
    else:
        log.info("embedded Dramatiq worker disabled (EMBEDDED_WORKER=false); a worker process must consume the queues")

    # Free the slots of scans whose worker died. Without this timer they stay "running" until another scan is
    # submitted or finishes, which may never happen.
    reaper = asyncio.create_task(_reap_loop(app.state.redis))

    log.info("Rogue App Discovery API started. DATA_DIR=%s", settings.data_dir)
    yield

    # Shutdown worker gracefully, then force-kill if needed
    if _worker_proc and _worker_proc.poll() is None:
        _worker_proc.terminate()
        try:
            _worker_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _worker_proc.kill()
        log.info("Dramatiq worker stopped.")

    # Cleanup
    reaper.cancel()
    await app.state.redis.aclose()
    await close_client()
    lock.release()
    log.info("API shutdown complete.")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Rogue App Discovery API",
        description="Lookalike / Rogue App Discovery Service",
        version="0.1.0",
        lifespan=_lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.include_router(scans_router)
    app.include_router(brand_details_router)

    @app.get("/health", tags=["health"])
    async def health(request: Request) -> dict:
        redis: aioredis.Redis = request.app.state.redis
        try:
            await redis.ping()
            redis_ok = True
        except Exception as exc:
            log.error("health check: redis ping failed: %s: %s", type(exc).__name__, exc)
            redis_ok = False
        return {
            "status": "ok" if redis_ok else "degraded",
            "redis": "ok" if redis_ok else "error",
        }

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled exception: %s", exc)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app


app = create_app()