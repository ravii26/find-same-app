# app/utils/rate_limiter.py
"""Per-domain rate limiter backed by Redis atomic counters.

Shared across all Dramatiq worker threads/processes via Redis.
Each domain gets a sliding-window counter with a configurable max RPS.

For rps >= 1: 1-second window, allow up to int(rps) requests.
For rps < 1:  window = round(1/rps) seconds, allow 1 request.
Example: rps=0.33 → 3-second window, 1 request (= 3s crawl-delay).
"""

from __future__ import annotations

import asyncio
import time

import redis.asyncio as aioredis


# Rates in requests-per-second. Values < 1.0 express a crawl-delay:
#   0.5  → 1 req / 2s
_DEFAULT_LIMITS: dict[str, float] = {
    "itunes.apple.com": 10,
    "apps.apple.com": 5,
    "play.google.com": 2,
    "uptodown.com": 1,
    "aptoide.com": 2,
    "mb-api.abuse.ch": 5,
    "urlhaus-api.abuse.ch": 2,
    "api.github.com": 10,
    "f-droid.org": 2,
    "t.me": 1,
    "lyzem.com": 1,
    "gitlab.com": 0.17,             # 10 req/min unauthenticated (≈1 per 6s)
    "virustotal.com": 0.067,    # 4 req/min free tier (≈1 per 15s)
    # Store and APK sites. The rendered (Playwright) and HTML scrapers run at 1 req/s:
    # they already get blocked at the default rate, and a slower pace keeps the IP off their block lists.
    "appgallery.huawei.com": 1,
    "galaxystore.samsung.com": 1,
    "global.app.mi.com": 2,         # GetApps JSON API
    "indusappstore.com": 1,
    "phonepeappstore.com": 1,       # the Indus/PhonePe store's live host
    "apkpure.com": 1,
    "apkmirror.com": 1,
    "apkcombo.com": 1,
    "apkfab.com": 1,
    "apk4fun.com": 1,
    "apkkit.com": 1,
    "apkboat.io": 1,
    "happymod.com": 1,
    "softonic.com": 1,
    "taptap.io": 1,
    "_default": 2,
}


class RateLimiter:
    def __init__(self, redis_client: aioredis.Redis) -> None:
        self._redis = redis_client

    def _rps(self, domain: str) -> float:
        base = domain.lower()
        if base.startswith("www."):  # only strip a literal www. prefix
            base = base[4:]
        for key, rps in _DEFAULT_LIMITS.items():
            if base == key or base.endswith(f".{key}"):
                return rps
        return _DEFAULT_LIMITS["_default"]

    def _window_and_limit(self, domain: str) -> tuple[int, int]:
        """Return (window_seconds, max_requests_per_window)."""
        rps = self._rps(domain)
        if rps >= 1.0:
            return 1, max(1, int(rps))
        # Sub-1 RPS: express as a longer window with 1 allowed request
        window = max(1, round(1.0 / rps))
        return window, 1

    def _key(self, domain: str, window: int) -> str:
        ts = int(time.time() / window)
        return f"ratelimit:{domain}:{ts}"

    async def acquire(self, domain: str, max_wait: float = 60.0) -> None:
        """Block until a rate-limit slot is available for this domain.

        Fails open after max_wait seconds so a saturated domain can never
        hang a scan indefinitely — the request proceeds and the source may
        then get a 403/429, which callers already handle gracefully.
        """
        window, limit = self._window_and_limit(domain)
        deadline = time.monotonic() + max_wait
        while True:
            key = self._key(domain, window)  # recompute — window bucket rolls over as we wait
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, window + 1)
            if count <= limit:
                return
            if time.monotonic() >= deadline:
                return  # fail open
            await asyncio.sleep(0.1)
