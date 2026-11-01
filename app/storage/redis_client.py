# app/storage/redis_client.py
"""Every Redis connection the app opens, built here from the configured URL.

Each caller gets the client shaped for its use:
- api_client(): the FastAPI process, one pool shared by all requests.
- worker_client(): one scan's client in the Dramatiq worker. aioredis connections are bound to the event loop
  that opened them, and each scan runs in its own asyncio.run(), so a client is never shared between scans.
- rate_limit_client(): the per-thread rate limiter. A bounded pool, so threads cannot open hundreds of sockets.

The Dramatiq broker (app/jobs/broker.py) reads the same URL but manages its own connections.
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.config.settings import get_settings


def api_client() -> aioredis.Redis:
    s = get_settings()
    pool = aioredis.ConnectionPool.from_url(s.redis_url, max_connections=s.redis_max_connections, decode_responses=False)
    return aioredis.Redis(connection_pool=pool)


def worker_client() -> aioredis.Redis:
    return aioredis.Redis.from_url(get_settings().redis_url, max_connections=5, decode_responses=False)


def rate_limit_client() -> aioredis.Redis:
    """A blocked acquire waits up to 2 s, then fails. The caller fails open (no throttling) on that error."""
    pool = aioredis.BlockingConnectionPool.from_url(get_settings().redis_url, max_connections=16, timeout=2)
    return aioredis.Redis(connection_pool=pool)
