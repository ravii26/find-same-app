"""Unit tests for the scan orchestrator."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.models import (
    FoundVia,
    Hit,
    HitType,
    HostInfo,
    ScanRequest,
    SourceMethod,
    SourceStatus,
)


def _make_request() -> ScanRequest:
    return ScanRequest(
        brand_name="Petpooja",
        android_packages=["com.petpooja.billing"],
        keywords=["petpooja"],
    )


def _make_hit(title: str = "Fake App") -> Hit:
    return Hit(
        id="abc123",
        type=HitType.app_listing,
        title=title,
        url="https://apkpure.com/fake",
        host=HostInfo(group="apk_site", name="APKPure", domain="apkpure.com"),
        found_via=[FoundVia(source="brave", method=SourceMethod.api, query="petpooja")],
        relevance_score=0,
        discovered_at=datetime.datetime.now(datetime.UTC),
    )


def _mock_adapter(name: str, hits: list[Hit]) -> MagicMock:
    adapter = MagicMock()
    adapter.name = name
    adapter.method = SourceMethod.api
    status = SourceStatus(name=name, method=SourceMethod.api, status="ok", results=len(hits))

    async def _run(ctx):
        return hits, status

    adapter._run = _run
    return adapter


@pytest.mark.asyncio
async def test_run_scan_returns_report():
    from app.services.orchestrator import run_scan

    redis = AsyncMock()
    request = _make_request()
    adapters = [_mock_adapter("brave", [_make_hit()])]

    with patch("app.services.orchestrator.get_all_adapters", return_value=adapters), \
         patch("app.services.orchestrator.status_store") as mock_ss:
        mock_ss.update_meta_field = AsyncMock()
        mock_ss.set_progress = AsyncMock()
        mock_ss.is_cancelled = AsyncMock(return_value=False)
        mock_ss.update_sources = AsyncMock()

        report = await run_scan("scn_001", request, redis)

    assert report.scan_id == "scn_001"
    assert report.summary.total_results >= 0


@pytest.mark.asyncio
async def test_run_scan_with_multiple_adapters():
    from app.services.orchestrator import run_scan

    redis = AsyncMock()
    request = _make_request()
    adapters = [
        _mock_adapter("brave", [_make_hit("App 1")]),
        _mock_adapter("itunes", [_make_hit("App 2"), _make_hit("App 3")]),
        _mock_adapter("github", []),
    ]

    with patch("app.services.orchestrator.get_all_adapters", return_value=adapters), \
         patch("app.services.orchestrator.status_store") as mock_ss:
        mock_ss.update_meta_field = AsyncMock()
        mock_ss.set_progress = AsyncMock()
        mock_ss.is_cancelled = AsyncMock(return_value=False)
        mock_ss.update_sources = AsyncMock()

        report = await run_scan("scn_002", request, redis)

    assert report.scan_id == "scn_002"
    assert len(report.sources) == 3


@pytest.mark.asyncio
async def test_run_scan_cancelled_mid_run():
    from app.services.orchestrator import run_scan

    redis = AsyncMock()
    request = _make_request()
    adapters = [_mock_adapter("brave", [])]

    with patch("app.services.orchestrator.get_all_adapters", return_value=adapters), \
         patch("app.services.orchestrator.status_store") as mock_ss:
        mock_ss.update_meta_field = AsyncMock()
        mock_ss.set_progress = AsyncMock()
        mock_ss.is_cancelled = AsyncMock(return_value=True)
        mock_ss.update_sources = AsyncMock()

        report = await run_scan("scn_003", request, redis)

    assert any("cancel" in w.lower() for w in report.warnings)


@pytest.mark.asyncio
async def test_run_scan_progress_callback():
    from app.services.orchestrator import run_scan

    redis = AsyncMock()
    request = _make_request()
    adapters = [_mock_adapter("brave", [_make_hit()])]
    progress_vals: list[int] = []

    def cb(pct):
        progress_vals.append(pct)

    with patch("app.services.orchestrator.get_all_adapters", return_value=adapters), \
         patch("app.services.orchestrator.status_store") as mock_ss:
        mock_ss.update_meta_field = AsyncMock()
        mock_ss.set_progress = AsyncMock()
        mock_ss.is_cancelled = AsyncMock(return_value=False)
        mock_ss.update_sources = AsyncMock()

        await run_scan("scn_005", request, redis, progress_cb=cb)

    assert len(progress_vals) > 0
    assert all(isinstance(v, int) for v in progress_vals)


@pytest.mark.asyncio
async def test_run_scan_summary_has_by_group():
    from app.services.orchestrator import run_scan

    redis = AsyncMock()
    request = _make_request()
    adapters = [_mock_adapter("brave", [_make_hit("App")])]

    with patch("app.services.orchestrator.get_all_adapters", return_value=adapters), \
         patch("app.services.orchestrator.status_store") as mock_ss:
        mock_ss.update_meta_field = AsyncMock()
        mock_ss.set_progress = AsyncMock()
        mock_ss.is_cancelled = AsyncMock(return_value=False)
        mock_ss.update_sources = AsyncMock()

        report = await run_scan("scn_006", request, redis)

    assert isinstance(report.summary.by_source_group, dict)
    assert report.summary.duration_ms >= 0
