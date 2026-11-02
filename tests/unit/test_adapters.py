"""Unit tests for source adapters using mocked HTTP responses."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.api.models import (
    ScanContext,
    ScanRequest,
    SourceMethod,
)


def _make_ctx(brand: str = "Petpooja", pkg: str = "com.petpooja.billing") -> ScanContext:
    req = ScanRequest(
        brand_name=brand,
        android_packages=[pkg],
        keywords=["petpooja"],
    )
    from app.services.keyword_expansion import expand
    kws = expand(req)
    return ScanContext(
        scan_id="scn_test",
        request=req,
        expanded_keywords=kws,
        max_pages_per_scan=2,
        max_response_bytes=5_000_000,
        depth=1,
    )


def _mock_response(content: bytes | str, status: int = 200) -> httpx.Response:
    if isinstance(content, str):
        content = content.encode()
    return httpx.Response(
        status,
        content=content,
        request=httpx.Request("GET", "https://example.com"),
    )


# ── iTunes adapter ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_itunes_returns_hits():
    from app.sources.itunes import ItunesAdapter

    data = {
        "resultCount": 1,
        "results": [{
            "trackId": 12345,
            "trackName": "Petpooja POS",
            "bundleId": "com.petpooja.pos",
            "trackViewUrl": "https://apps.apple.com/us/app/petpooja-pos/id12345",
            "sellerName": "Petpooja Technologies",
            "version": "5.1.0",
        }]
    }
    resp = _mock_response(json.dumps(data).encode())

    with patch("app.sources.itunes.fetch", new=AsyncMock(return_value=resp)):
        adapter = ItunesAdapter()
        ctx = _make_ctx()
        hits, status = await adapter.search(ctx)

    assert len(hits) > 0
    assert hits[0].title == "Petpooja POS"
    assert status.status == "ok"


@pytest.mark.asyncio
async def test_itunes_fetch_exception_skipped():
    from app.sources.itunes import ItunesAdapter
    from app.utils.http_client import FetchError

    with patch("app.sources.itunes.fetch", new=AsyncMock(side_effect=FetchError("timeout"))):
        adapter = ItunesAdapter()
        hits, status = await adapter.search(_make_ctx())

    # When every query errors and nothing is found, the source now reports
    # "failed" (surfaced to the user) rather than silently claiming "ok".
    assert hits == []
    assert status.status == "failed"
    assert status.error


@pytest.mark.asyncio
async def test_itunes_dedup_same_url():
    from app.sources.itunes import ItunesAdapter

    item = {
        "trackId": 111,
        "trackName": "Petpooja Clone",
        "bundleId": "com.fake.app",
        "trackViewUrl": "https://apps.apple.com/app/id111",
        "version": "1.0",
    }
    data = {"resultCount": 1, "results": [item]}
    resp = _mock_response(json.dumps(data).encode())

    with patch("app.sources.itunes.fetch", new=AsyncMock(return_value=resp)):
        adapter = ItunesAdapter()
        ctx = _make_ctx()
        ctx.request = ScanRequest(
            brand_name="Petpooja",
            keywords=["petpooja"],
        )
        from app.services.keyword_expansion import expand
        ctx.expanded_keywords = expand(ctx.request)
        hits, _ = await adapter.search(ctx)

    ids = [h.id for h in hits]
    assert len(ids) == len(set(ids)), "Duplicate hits from same URL"


# ── F-Droid adapter ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fdroid_finds_matching_package():
    from app.sources.fdroid_adapter import FDroidAdapter
    FDroidAdapter._index_cache = None  # reset cache

    index = {
        "packages": {
            "com.petpooja.fake": {
                "metadata": {
                    "name": {"en-US": "Petpooja Fake"},
                },
                "versions": {
                    "v1": {"manifest": {"versionCode": 1, "versionName": "1.0"}},
                },
            },
            "com.other.app": {
                "metadata": {
                    "name": {"en-US": "Something Else"},
                },
                "versions": {},
            },
        }
    }
    resp = _mock_response(json.dumps(index).encode())

    with patch("app.sources.fdroid_adapter.fetch", new=AsyncMock(return_value=resp)):
        adapter = FDroidAdapter()
        hits, status = await adapter.search(_make_ctx())

    hit_pkgs = [h.package_id for h in hits]
    assert "com.petpooja.fake" in hit_pkgs
    assert "com.other.app" not in hit_pkgs


@pytest.mark.asyncio
async def test_fdroid_fetch_error_returns_failed():
    from app.sources.fdroid_adapter import FDroidAdapter
    from app.utils.http_client import FetchError
    FDroidAdapter._index_cache = None

    with patch("app.sources.fdroid_adapter.fetch", new=AsyncMock(side_effect=FetchError("network"))):
        adapter = FDroidAdapter()
        hits, status = await adapter.search(_make_ctx())

    assert hits == []
    assert status.status == "failed"


@pytest.mark.asyncio
async def test_fdroid_uses_cache():
    from app.sources.fdroid_adapter import FDroidAdapter
    FDroidAdapter._index_cache = None

    index = {"packages": {}}
    resp = _mock_response(json.dumps(index).encode())

    mock_fetch = AsyncMock(return_value=resp)
    with patch("app.sources.fdroid_adapter.fetch", mock_fetch):
        adapter = FDroidAdapter()
        await adapter._get_index()
        await adapter._get_index()

    assert mock_fetch.call_count == 1  # second call uses cache


# ── GitHub adapter ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_github_returns_repo_hits():
    from app.sources.github_adapter import GitHubAdapter

    data = {
        "total_count": 1,
        "items": [{
            "html_url": "https://github.com/hacker/petpooja-clone",
            "full_name": "hacker/petpooja-clone",
            "name": "petpooja-clone",
            "owner": {"login": "hacker"},
        }]
    }
    resp = _mock_response(json.dumps(data).encode())

    with patch("app.sources.github_adapter.fetch", new=AsyncMock(return_value=resp)), \
         patch("app.sources.github_adapter.get_settings") as mock_gs:
        mock_gs.return_value = MagicMock(github_token="")
        adapter = GitHubAdapter()
        hits, status = await adapter.search(_make_ctx())

    assert len(hits) >= 1
    assert hits[0].url == "https://github.com/hacker/petpooja-clone"
    assert status.status == "ok"


@pytest.mark.asyncio
async def test_github_fetch_error_continues():
    from app.sources.github_adapter import GitHubAdapter
    from app.utils.http_client import FetchError

    with patch("app.sources.github_adapter.fetch", new=AsyncMock(side_effect=FetchError("err"))), \
         patch("app.sources.github_adapter.get_settings") as mock_gs:
        mock_gs.return_value = MagicMock(github_token="")
        adapter = GitHubAdapter()
        hits, status = await adapter.search(_make_ctx())

    # All queries errored with no hits → "failed" (surfaced), not a silent "ok".
    assert hits == []
    assert status.status == "failed"
    assert status.error


# ── Base adapter _run wrapper ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_base_run_catches_exceptions():
    from app.sources.base import SourceAdapter

    class BrokenAdapter(SourceAdapter):
        name = "broken"
        method = SourceMethod.api
        host_group = "apk_site"

        async def search(self, ctx):
            raise ValueError("something went wrong")

    adapter = BrokenAdapter()
    hits, status = await adapter._run(_make_ctx())
    assert hits == []
    assert status.status == "failed"
    assert "ValueError" in status.error


@pytest.mark.asyncio
async def test_base_run_timing():
    from app.sources.base import SourceAdapter
    from app.api.models import SourceStatus

    class FastAdapter(SourceAdapter):
        name = "fast"
        method = SourceMethod.api
        host_group = "apk_site"

        async def search(self, ctx):
            return [], SourceStatus(name="fast", method=SourceMethod.api, status="ok")

    adapter = FastAdapter()
    hits, status = await adapter._run(_make_ctx())
    assert status.duration_ms is not None
    assert status.duration_ms >= 0
