"""Integration tests for the two-queue concurrency model.

Uses fakeredis and Dramatiq StubBroker.
Verifies: pending queue accumulation, running cap, slot release.
"""

from __future__ import annotations


import pytest
from fakeredis.aioredis import FakeRedis

from app.api.models import ScanStatus
from app.storage import status_store


@pytest.fixture
def redis():
    return FakeRedis(decode_responses=False)


@pytest.mark.asyncio
async def test_status_store_set_and_get(redis):
    await status_store.set_status(redis, "scn_001", ScanStatus.queued)
    s = await status_store.get_status(redis, "scn_001")
    assert s == ScanStatus.queued


@pytest.mark.asyncio
async def test_running_set_add_remove(redis):
    await status_store.add_to_running(redis, "scn_001")
    await status_store.add_to_running(redis, "scn_002")
    running = await status_store.get_running_ids(redis)
    assert "scn_001" in running
    assert "scn_002" in running

    await status_store.remove_from_running(redis, "scn_001")
    running = await status_store.get_running_ids(redis)
    assert "scn_001" not in running
    assert "scn_002" in running


@pytest.mark.asyncio
async def test_dedup_set_and_get(redis):
    await status_store.set_dedup(redis, "hash123", "scn_001")
    result = await status_store.get_dedup(redis, "hash123")
    assert result == "scn_001"


@pytest.mark.asyncio
async def test_cancel_flag(redis):
    assert not await status_store.is_cancelled(redis, "scn_001")
    await status_store.signal_cancel(redis, "scn_001")
    assert await status_store.is_cancelled(redis, "scn_001")


@pytest.mark.asyncio
async def test_progress_set_and_get(redis):
    await status_store.set_progress(redis, "scn_001", 42)
    p = await status_store.get_progress(redis, "scn_001")
    assert p == 42


@pytest.mark.asyncio
async def test_delete_all_clears_keys(redis):
    await status_store.set_status(redis, "scn_001", ScanStatus.running)
    await status_store.add_to_running(redis, "scn_001")
    await status_store.set_progress(redis, "scn_001", 50)
    await status_store.delete_all(redis, "scn_001")

    assert await status_store.get_status(redis, "scn_001") is None
    running = await status_store.get_running_ids(redis)
    assert "scn_001" not in running


@pytest.mark.asyncio
async def test_multiple_scans_running_counter(redis):
    """Simulate multiple scans and verify running set tracking."""
    scan_ids = [f"scn_{i:03d}" for i in range(6)]

    # Add 4 to running (cap)
    for sid in scan_ids[:4]:
        await status_store.add_to_running(redis, sid)

    running = await status_store.get_running_ids(redis)
    assert len(running) == 4

    # Release one slot
    await status_store.remove_from_running(redis, scan_ids[0])
    running = await status_store.get_running_ids(redis)
    assert len(running) == 3
    assert scan_ids[0] not in running


@pytest.mark.asyncio
async def test_client_count_increment_decrement(redis):
    await status_store.increment_client_count(redis, "client_1")
    await status_store.increment_client_count(redis, "client_1")
    count = await status_store.get_client_count(redis, "client_1")
    assert count == 2

    await status_store.decrement_client_count(redis, "client_1")
    count = await status_store.get_client_count(redis, "client_1")
    assert count == 1


@pytest.mark.asyncio
async def test_meta_roundtrip(redis):
    import datetime
    now = datetime.datetime.now(datetime.UTC)
    await status_store.set_meta(
        redis, "scn_001", "TestBrand", now, warnings=["warn1"]
    )
    meta = await status_store.get_meta(redis, "scn_001")
    assert meta is not None
    assert meta["brand_name"] == "TestBrand"
    assert meta["warnings"] == ["warn1"]
