# app/jobs/broker.py
"""Dramatiq broker configuration."""

from __future__ import annotations

import dramatiq
from dramatiq.brokers.redis import RedisBroker
from dramatiq.middleware import Retries, TimeLimit

from app.config.settings import get_settings

_broker: RedisBroker | None = None


def get_broker() -> RedisBroker:
    global _broker
    if _broker is None:
        s = get_settings()
        _broker = RedisBroker(
            url=s.redis_url,
            namespace="rogue_scanner",
            heartbeat_timeout=60_000,
            dead_message_ttl=604_800_000,
            middleware=[
                Retries(max_retries=3, min_backoff=1_000, max_backoff=30_000),
                TimeLimit(time_limit=s.scan_ceiling_seconds() * 1_000 + 120_000),
            ],
        )
        dramatiq.set_broker(_broker)
    return _broker