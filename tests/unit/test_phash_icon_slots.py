"""Icon downloads use a semaphore per event loop: each scan runs in its own asyncio.run() loop."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

from PIL import Image

from app.utils import phash


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_contended_icon_downloads_work_in_successive_loops(monkeypatch):
    async def fake_fetch(url, **_):
        await asyncio.sleep(0)  # yield so the 20 tasks contend for the 16 slots
        return SimpleNamespace(content=_png())

    monkeypatch.setattr(phash, "fetch", fake_fetch)

    async def run_batch():
        urls = [f"https://img.example/{i}.png" for i in range(20)]
        return await asyncio.gather(*[phash.compute_phash(u) for u in urls])

    for _ in range(2):  # a second loop used to fail with "bound to a different event loop"
        hashes = asyncio.run(run_batch())
        assert all(h for h in hashes)


def test_each_loop_gets_its_own_semaphore():
    async def grab():
        return phash._icon_sem()

    first = asyncio.run(grab())
    second = asyncio.run(grab())
    assert first is not second
