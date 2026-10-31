"""Integration test fixtures.

Patches FakeConnection to skip HELLO negotiation, which redis-py 8.x sends
by default (RESP3) but fakeredis 2.26.1 does not implement.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True, scope="session")
def patch_fakeredis_no_hello():
    """Make FakeConnection skip HELLO so it works with redis-py 8.x."""
    from fakeredis.aioredis import FakeConnection

    async def _minimal_health_check(self, check_health: bool = True) -> None:
        self._reset_himport_state()
        self._parser.on_connect(self)

    original = FakeConnection.on_connect_check_health
    FakeConnection.on_connect_check_health = _minimal_health_check
    yield
    FakeConnection.on_connect_check_health = original
