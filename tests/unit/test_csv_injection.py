"""Unit tests for CSV formula injection neutralization."""

from __future__ import annotations

import csv
import pytest
import io
import datetime

from app.api.models import (
    FoundVia,
    Hit,
    HitType,
    HostInfo,
    ScanQuery,
    ScanReport,
    ScanSummary,
    SourceMethod,
)
from app.api.routes.scans import _stream_csv


def _report_with_hit(title: str) -> ScanReport:
    hit = Hit(
        id="abc",
        type=HitType.app_listing,
        title=title,
        url="https://example.com",
        host=HostInfo(group="apk_site", name="Test", domain="example.com"),
        found_via=[FoundVia(source="brave", method=SourceMethod.api, query="q")],
        relevance_score=50,
        discovered_at=datetime.datetime.now(datetime.UTC),
    )
    return ScanReport(
        scan_id="scn_test",
        query=ScanQuery(
            brand_name="Test", keywords=["test"],
            received_at=datetime.datetime.now(datetime.UTC),
        ),
        summary=ScanSummary(total_results=1, scoring_ruleset="v1"),
        results=[hit],
    )


async def _get_csv_cells(response) -> list[list[str]]:
    content = b""
    async for chunk in response.body_iterator:
        content += chunk.encode() if isinstance(chunk, str) else chunk
    reader = csv.reader(io.StringIO(content.decode("utf-8-sig")))
    return list(reader)


@pytest.mark.asyncio
async def test_equals_prefix_neutralized():
    report = _report_with_hit("=SUM(1+1)")
    resp = _stream_csv(report)
    rows = await _get_csv_cells(resp)
    header = rows[0]
    title_idx = header.index("title")
    data_row = rows[1]
    assert not data_row[title_idx].startswith("="), "Formula injection not neutralized"
    assert data_row[title_idx].startswith("'=")


@pytest.mark.asyncio
async def test_plus_prefix_neutralized():
    report = _report_with_hit("+cmd|' /C calc'!A0")
    resp = _stream_csv(report)
    rows = await _get_csv_cells(resp)
    title_idx = rows[0].index("title")
    assert rows[1][title_idx].startswith("'")


@pytest.mark.asyncio
async def test_at_prefix_neutralized():
    report = _report_with_hit("@SUM(1)")
    resp = _stream_csv(report)
    rows = await _get_csv_cells(resp)
    title_idx = rows[0].index("title")
    assert rows[1][title_idx].startswith("'")


@pytest.mark.asyncio
async def test_normal_title_not_prefixed():
    report = _report_with_hit("Normal App Name")
    resp = _stream_csv(report)
    rows = await _get_csv_cells(resp)
    title_idx = rows[0].index("title")
    assert rows[1][title_idx] == "Normal App Name"
