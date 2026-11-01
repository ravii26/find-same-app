"""Discovery from an App Store link, alone or with a Play link, and the request rule that one link is required."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.routes.brand_details import BrandDetailsRequest
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





def test_request_accepts_either_link_alone():
    assert BrandDetailsRequest(brand_name="YONO SBI", app_store_urls=[APP_STORE]).play_store_urls == []
    assert BrandDetailsRequest(brand_name="YONO SBI", play_store_urls=[PLAY]).app_store_urls == []


def test_request_accepts_both_links():
    body = BrandDetailsRequest(brand_name="YONO SBI", play_store_urls=[PLAY], app_store_urls=[APP_STORE])
    assert body.play_store_urls == [PLAY] and body.app_store_urls == [APP_STORE]


def test_request_needs_one_link():
    with pytest.raises(ValidationError):
        BrandDetailsRequest(brand_name="YONO SBI")


def test_request_rejects_a_non_app_store_link_in_the_app_store_field():
    with pytest.raises(ValidationError):
        BrandDetailsRequest(brand_name="YONO SBI", app_store_urls=[PLAY])
