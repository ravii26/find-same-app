"""Samsung Galaxy Store: the app's detail call fills version, seller and description on the hit."""

from __future__ import annotations

import datetime
import json

import httpx
import pytest

from app.api.models import FoundVia, Hit, HitType, HostInfo
from app.sources import samsung_adapter
from app.sources.samsung_adapter import SamsungAdapter
from app.utils.http_client import FetchError


def _hit(publisher: str | None = None) -> Hit:
    return Hit(
        id="s1", type=HitType.app_listing, url="https://galaxystore.samsung.com/detail/com.kreditbee.android",
        app_name="KreditBee", package_id="com.kreditbee.android", publisher=publisher,
        host=HostInfo(group="app_store", name="Samsung Galaxy Store", domain="galaxystore.samsung.com"),
        found_via=[FoundVia(source="samsung_galaxy", method="api", query="KreditBee")],
        relevance_score=0, discovered_at=datetime.datetime.now(datetime.UTC),
    )


DETAIL = {"DetailMain": {"contentBinaryVersion": "2.3.5", "contentBinarySize": "16.52 MB",
                         "sellerName": "Krazybee Services Limited ", "contentDescription": "KreditBee is a loan app. " * 100}}


async def test_detail_fills_version_seller_and_description(monkeypatch):
    async def fake_fetch(url, **_):
        assert url == "https://galaxystore.samsung.com/api/detail/com.kreditbee.android"
        return httpx.Response(200, text=json.dumps(DETAIL), headers={"content-type": "application/json"})

    async def no_throttle(url):
        return None

    monkeypatch.setattr(samsung_adapter, "fetch", fake_fetch)
    monkeypatch.setattr(samsung_adapter, "throttle", no_throttle)
    hit = await SamsungAdapter._with_detail(_hit())
    assert hit.version == "2.3.5"
    assert hit.publisher == "Krazybee Services Limited"
    assert hit.description == DETAIL["DetailMain"]["contentDescription"].strip()[:1000]
    assert len(hit.description) == 1000


async def test_seller_from_search_is_kept(monkeypatch):
    async def fake_fetch(url, **_):
        return httpx.Response(200, text=json.dumps(DETAIL))

    async def no_throttle(url):
        return None

    monkeypatch.setattr(samsung_adapter, "fetch", fake_fetch)
    monkeypatch.setattr(samsung_adapter, "throttle", no_throttle)
    hit = await SamsungAdapter._with_detail(_hit(publisher="Krazybee Services Limited"))
    assert hit.publisher == "Krazybee Services Limited"


async def test_failed_detail_call_keeps_the_search_fields(monkeypatch):
    async def failing_fetch(url, **_):
        raise FetchError("timeout")

    async def no_throttle(url):
        return None

    monkeypatch.setattr(samsung_adapter, "fetch", failing_fetch)
    monkeypatch.setattr(samsung_adapter, "throttle", no_throttle)
    original = _hit(publisher="X")
    assert await SamsungAdapter._with_detail(original) == original
