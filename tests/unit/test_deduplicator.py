"""Unit tests for hit deduplication."""

from __future__ import annotations

import datetime

from app.api.models import FoundVia, Hit, HitType, HostInfo, SourceMethod
from app.services.deduplicator import deduplicate


def _hit(url: str, pkg: str | None = None, source: str = "brave") -> Hit:
    return Hit(
        id=url[:32],
        type=HitType.app_listing,
        url=url,
        package_id=pkg,
        host=HostInfo(group="apk_site", name="Test", domain="test.com"),
        found_via=[FoundVia(source=source, method=SourceMethod.scrape, query="q")],
        relevance_score=50,
        discovered_at=datetime.datetime.now(datetime.UTC),
    )


def test_deduplicate_same_url():
    hits = [
        _hit("https://example.com/app", source="brave"),
        _hit("https://example.com/app", source="github"),
    ]
    result = deduplicate(hits)
    assert len(result) == 1
    # found_via should be merged
    assert len(result[0].found_via) == 2


def test_deduplicate_different_urls():
    hits = [
        _hit("https://example.com/app1"),
        _hit("https://example.com/app2"),
    ]
    result = deduplicate(hits)
    assert len(result) == 2


def test_deduplicate_same_package_id():
    hits = [
        _hit("https://site1.com/app", pkg="com.example.app"),
        _hit("https://site2.com/app", pkg="com.example.app"),
    ]
    result = deduplicate(hits)
    assert len(result) == 1


def test_deduplicate_tracking_params_same():
    hits = [
        _hit("https://example.com/app?utm_source=x"),
        _hit("https://example.com/app?utm_source=y"),
    ]
    result = deduplicate(hits)
    assert len(result) == 1


def test_deduplicate_preserves_order():
    urls = [f"https://example.com/app{i}" for i in range(5)]
    hits = [_hit(u) for u in urls]
    result = deduplicate(hits)
    assert [h.url for h in result] == urls


def test_deduplicate_empty():
    assert deduplicate([]) == []


def test_deduplicate_no_merge_different_source_same_query():
    hits = [
        _hit("https://example.com/app", source="brave"),
        _hit("https://example.com/app", source="brave"),  # same source, same query
    ]
    result = deduplicate(hits)
    assert len(result) == 1
    # Should not duplicate the same found_via
    sources = [fv.source for fv in result[0].found_via]
    assert sources.count("brave") == 1
