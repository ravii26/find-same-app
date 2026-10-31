"""
Verifies that CPU-heavy fuzzy scoring does not block the event loop.

Runs a slow scoring task (large batch) concurrently with a trivial coroutine
and asserts the trivial coroutine finishes quickly.
"""

from __future__ import annotations

import asyncio
import datetime
import time

import pytest

from app.api.models import (
    FoundVia,
    Hit,
    HitType,
    HostInfo,
    ScanRequest,
    SourceMethod,
)
from app.services.scoring import score_hits


def _make_hits(n: int) -> list[Hit]:
    return [
        Hit(
            id=f"hit_{i}",
            type=HitType.app_listing,
            title=f"Petpooja Variant {i}",
            app_name=f"Petpooja App {i}",
            url=f"https://apksite.com/app{i}",
            host=HostInfo(group="apk_site", name="APKSite", domain="apksite.com"),
            found_via=[FoundVia(source="brave", method=SourceMethod.api, query="petpooja")],
            relevance_score=0,
            discovered_at=datetime.datetime.now(datetime.UTC),
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_scoring_does_not_block_event_loop():
    """Score 50 hits while a lightweight coroutine runs; verify the light one finishes in < 200ms."""
    request = ScanRequest(
        brand_name="Petpooja",
        android_packages=["com.petpooja.billing"],
        keywords=["petpooja"],
        official_domain="petpooja.com",
    )
    hits = _make_hits(50)

    api_latencies: list[float] = []

    async def lightweight_task():
        for _ in range(5):
            t0 = time.monotonic()
            await asyncio.sleep(0)  # yield to event loop
            latency = (time.monotonic() - t0) * 1000
            api_latencies.append(latency)

    # Run both concurrently
    await asyncio.gather(
        score_hits(hits, request),
        lightweight_task(),
    )

    # P99 of our latency samples should be well under 200ms
    api_latencies.sort()
    p99 = api_latencies[int(len(api_latencies) * 0.99)]
    assert p99 < 200, (
        f"Event loop was blocked: lightweight coroutine P99 latency = {p99:.1f}ms (max 200ms)"
    )
