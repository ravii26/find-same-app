"""Discovery from an App Store link, alone or with a Play link, and the request rule that one link is required."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.routes.brands import DiscoverRequest
from app.services import discovery

PLAY = "https://play.google.com/store/apps/details?id=com.sbi.lotusintouch"
APP_STORE = "https://apps.apple.com/in/app/yono-sbi-bangladesh/id1111?l=en"
SELLER = 503784436

SELLER_APPS = [
    {"kind": "software", "bundleId": "com.sbi.YONOBD", "trackName": "YONO SBI Bangladesh",
     "artistName": "State Bank of India", "artworkUrl60": "https://img/ios1"},
    {"kind": "software", "bundleId": "com.sbi.sbiquick.ios", "trackName": "SBI Quick",
     "artistName": "State Bank of India", "artworkUrl60": "https://img/ios2"},
]


def _fake_itunes(monkeypatch):
    """The app id 1111 belongs to the seller; the seller id lists the seller's software."""
    async def fake_json(url):
        if f"id={1111}&" in url:
            return {"results": [{"kind": "software", "trackId": 1111, "artistId": SELLER,
                                 "artistName": "State Bank of India", "bundleId": "com.sbi.YONOBD",
                                 "trackName": "YONO SBI Bangladesh"}]}
        if f"id={SELLER}&" in url:
            return {"results": SELLER_APPS}
        return {"results": []}

    monkeypatch.setattr(discovery, "_itunes_json", fake_json)

    async def no_search(terms, cc, notes):
        return []

    monkeypatch.setattr(discovery, "_name_search_candidates", no_search)


def test_app_id_is_read_from_app_store_link():
    assert discovery.app_store_id_from_url(APP_STORE) == 1111


@pytest.mark.parametrize("url", [
    "https://play.google.com/store/apps/details?id=com.sbi.lotusintouch",
    "https://apps.apple.com/in/app/name",
])
def test_non_app_store_links_have_no_app_id(url):
    assert discovery.app_store_id_from_url(url) is None


async def test_app_store_only_discovers_the_sellers_apps(monkeypatch):
    _fake_itunes(monkeypatch)
    result = await discovery.discover(None, APP_STORE, "YONO SBI", [], "in")

    assert result["play_package"] is None
    assert result["play_developer"] == "State Bank of India"
    assert {(c["store"], c["id"]) for c in result["candidates"]} == {
        ("app_store", "com.sbi.YONOBD"), ("app_store", "com.sbi.sbiquick.ios"),
    }
    assert any("no Google Play link" in n for n in result["notes"])


async def test_no_link_is_an_error():
    with pytest.raises(discovery.DiscoveryError):
        await discovery.discover(None, None, "YONO SBI", [], "in")


async def test_unknown_app_store_app_is_an_error(monkeypatch):
    async def empty(url):
        return {"results": []}

    monkeypatch.setattr(discovery, "_itunes_json", empty)
    with pytest.raises(discovery.DiscoveryError):
        await discovery.discover(None, APP_STORE, "YONO SBI", [], "in")


def test_request_accepts_either_link_alone():
    assert DiscoverRequest(brand_name="YONO SBI", app_store_url=APP_STORE).play_store_url is None
    assert DiscoverRequest(brand_name="YONO SBI", play_store_url=PLAY).app_store_url is None


def test_request_accepts_both_links():
    body = DiscoverRequest(brand_name="YONO SBI", play_store_url=PLAY, app_store_url=APP_STORE)
    assert body.play_store_url == PLAY and body.app_store_url == APP_STORE


def test_request_needs_one_link():
    with pytest.raises(ValidationError):
        DiscoverRequest(brand_name="YONO SBI")


def test_request_rejects_a_non_app_store_link_in_the_app_store_field():
    with pytest.raises(ValidationError):
        DiscoverRequest(brand_name="YONO SBI", app_store_url=PLAY)
