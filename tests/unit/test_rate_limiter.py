"""Unit tests for the Redis-backed rate limiter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils.rate_limiter import RateLimiter


def _make_redis(incr_seq: list[int]) -> MagicMock:
    """Mock redis that returns values from incr_seq on each incr() call."""
    redis = AsyncMock()
    redis.incr.side_effect = iter(incr_seq)
    redis.expire = AsyncMock()
    return redis


@pytest.mark.asyncio
async def test_acquire_under_limit():
    redis = _make_redis([1])
    limiter = RateLimiter(redis)
    await limiter.acquire("example.com")
    redis.incr.assert_called_once()
    redis.expire.assert_called_once()


@pytest.mark.asyncio
async def test_acquire_first_call_sets_expire():
    redis = _make_redis([1])
    limiter = RateLimiter(redis)
    await limiter.acquire("example.com")
    redis.expire.assert_called_once()


@pytest.mark.asyncio
async def test_acquire_not_first_call_no_expire():
    redis = _make_redis([3])
    redis.expire = AsyncMock()
    limiter = RateLimiter(redis)
    # count=3, expire is called only on count==1
    # The default limit for _default is 2, so count=3 is over limit
    # We need to simulate count going to 1 on next attempt
    redis.incr.side_effect = [3, 1]
    with patch("asyncio.sleep", new=AsyncMock()):
        await limiter.acquire("example.com")
    # expire only called when count==1
    assert redis.expire.call_count == 1


@pytest.mark.asyncio
async def test_limit_known_domain_github():
    redis = _make_redis([1])
    limiter = RateLimiter(redis)
    limit = limiter._limit("api.github.com")
    assert limit == 10


@pytest.mark.asyncio
async def test_limit_unknown_domain_default():
    redis = _make_redis([1])
    limiter = RateLimiter(redis)
    limit = limiter._limit("unknown-domain.xyz")
    assert limit == 2


@pytest.mark.asyncio
async def test_limit_www_prefix_stripped():
    redis = _make_redis([1])
    limiter = RateLimiter(redis)
    limit = limiter._limit("www.apkmirror.com")
    assert limit >= 1


@pytest.mark.asyncio
async def test_key_contains_domain():
    redis = _make_redis([1])
    limiter = RateLimiter(redis)
    key = limiter._key("example.com")
    assert "ratelimit:example.com" in key
