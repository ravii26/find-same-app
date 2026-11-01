"""Brand discovery and the brand record: link parsing, candidate rules, confirmation and scan merge."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.routes.brand_details import BrandDetailsRequest
from app.services import discovery

PLAY = "https://play.google.com/store/apps/details?id=com.sbi.lotusintouch"


# ── link parsing and name matching ────────────────────────────────────────────

def test_package_is_read_from_play_link():
    assert discovery.play_package_from_url(PLAY) == "com.sbi.lotusintouch"


@pytest.mark.parametrize("url", [
    "https://apps.apple.com/us/app/x/id1",
    "https://play.google.com/store/apps/developer?id=State+Bank+of+India",
    "https://play.google.com/store/apps/details",
    "https://play.google.com/store/apps/details?id=notapackage",
])
def test_non_listing_links_are_rejected(url):
    assert discovery.play_package_from_url(url) is None


def test_discover_request_rejects_a_non_play_link():
    with pytest.raises(ValidationError):
        BrandDetailsRequest(brand_name="YONO SBI", play_store_url="https://example.com/app")




# ── discovery with the network replaced by fakes ──────────────────────────────

PLAY_LISTINGS = {
    "com.sbi.lotusintouch": {"title": "YONO SBI: Banking, UPI & Pay", "developer": "State Bank of India",
                             "developerId": "State+Bank+of+India", "icon": "https://img/1"},
    "com.YONOMUApp": {"title": "YONO SBI Mauritius", "developer": "State Bank of India", "icon": "https://img/2"},
    "com.sbi.myhrms": {"title": "SBI HRMS", "developer": "State Bank of India", "icon": "https://img/3"},
    "com.sbi.fi.app": {"title": "Google Home", "developer": "State Bank of India", "icon": "https://img/4"},
}
ITUNES = {
    "https://itunes.apple.com/search": [
        {"artistName": "State Bank of India", "artistId": 503784436},
        {"artistName": "SBI Other Developer", "artistId": 999},
    ],
    "https://itunes.apple.com/lookup": [
        {"kind": "software", "bundleId": "com.sbi.YONOBD", "trackName": "YONO SBI Bangladesh",
         "artistName": "State Bank of India", "artworkUrl60": "https://img/ios1"},
        {"kind": "software", "bundleId": "com.sbi.sbiquick.ios", "trackName": "SBI Quick",
         "artistName": "State Bank of India", "artworkUrl60": "https://img/ios2"},
    ],
}


def _patch_network(monkeypatch):
    async def no_search(terms, cc, notes):
        return []

    monkeypatch.setattr(discovery, "_name_search_candidates", no_search)

    async def fake_listing(pkg, cc):
        return PLAY_LISTINGS.get(pkg)

    async def fake_dev_packages(dev_id, cc, notes):
        return list(PLAY_LISTINGS)

    async def fake_itunes(url):
        for prefix, data in ITUNES.items():
            if url.startswith(prefix):
                return {"results": data}
        return {"results": []}

    monkeypatch.setattr(discovery, "_play_listing", fake_listing)
    monkeypatch.setattr(discovery, "_play_developer_packages", fake_dev_packages)
    monkeypatch.setattr(discovery, "_itunes_json", fake_itunes)






# ── developer page: browser scroll, static fallback ───────────────────────────




# ── several links per store ───────────────────────────────────────────────────

YONO_MU = "https://play.google.com/store/apps/details?id=com.YONOMUApp"



def test_single_link_field_is_added_to_the_list():
    body = BrandDetailsRequest(brand_name="YONO SBI", play_store_url=PLAY)
    assert body.play_store_urls == [PLAY]


def test_each_list_entry_is_checked():
    with pytest.raises(ValidationError):
        BrandDetailsRequest(brand_name="YONO SBI", play_store_urls=[PLAY, "https://example.com/app"])


# ── the brand's own store links, read at scan time ────────────────────────────

async def test_listed_app_reads_publisher_from_a_play_link(monkeypatch):
    async def listing(pkg, cc):
        return {"developer": "State Bank of India", "developerId": "State+Bank+of+India", "title": "YONO SBI"}

    monkeypatch.setattr(discovery, "_play_listing", listing)
    app = await discovery.listed_app(PLAY, "in")
    assert app == {"store": "play", "id": "com.sbi.lotusintouch", "publisher": "State Bank of India",
                   "developer_id": "State+Bank+of+India"}


async def test_listed_app_reads_bundle_and_seller_from_an_app_store_link(monkeypatch):
    async def lookup(aid, cc):
        return {"trackId": aid, "bundleId": "com.sbi.YONOBD", "artistName": "State Bank of India",
                "artistId": 503784436}

    monkeypatch.setattr(discovery, "_itunes_app", lookup)
    app = await discovery.listed_app("https://apps.apple.com/in/app/yono-sbi-bangladesh/id1556426905", "in")
    assert app == {"store": "app_store", "id": "com.sbi.YONOBD", "publisher": "State Bank of India",
                   "developer_id": "503784436"}


async def test_unreadable_listed_app_is_a_discovery_error(monkeypatch):
    async def missing(pkg, cc):
        return None

    monkeypatch.setattr(discovery, "_play_listing", missing)
    with pytest.raises(discovery.DiscoveryError):
        await discovery.listed_app(PLAY, "in")
