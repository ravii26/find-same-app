"""Brand discovery and the brand record: link parsing, candidate rules, confirmation and scan merge."""

from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from app.api.models import ScanRequest
from app.api.routes.brands import DiscoverRequest
from app.services import brand_store, discovery

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
        DiscoverRequest(brand_name="YONO SBI", play_store_url="https://example.com/app")


def test_name_match_is_whole_phrase_so_google_home_is_not_google_pay():
    assert discovery.name_matches("Google Pay for Business", ["Google Pay"])
    assert not discovery.name_matches("Google Home", ["Google Pay"])


def test_alias_counts_as_brand_name():
    assert discovery.name_matches("Yono Business", ["YONO SBI", "Yono"])
    assert discovery.name_matches("YONO SBI Sri Lanka", ["YONO SBI"])


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


async def test_discovery_builds_candidates_per_store(monkeypatch):
    _patch_network(monkeypatch)
    result = await discovery.discover(PLAY, None, "YONO SBI", aliases=["YONO"], cc="in")
    by_key = {(c["store"], c["id"]): c for c in result["candidates"]}

    assert ("play", "com.sbi.lotusintouch") in by_key
    assert ("play", "com.YONOMUApp") in by_key
    assert ("app_store", "com.sbi.YONOBD") in by_key
    assert result["play_developer"] == "State Bank of India"


async def test_only_brand_named_candidates_are_preselected(monkeypatch):
    _patch_network(monkeypatch)
    result = await discovery.discover(PLAY, None, "YONO SBI", aliases=[], cc="in")
    selected = {c["id"] for c in result["candidates"] if c["default"]}
    assert "com.sbi.lotusintouch" in selected
    assert "com.YONOMUApp" in selected
    assert "com.sbi.myhrms" not in selected  # listed for review, not pre-selected


async def test_app_store_seller_is_matched_by_name_only(monkeypatch):
    _patch_network(monkeypatch)
    result = await discovery.discover(PLAY, None, "YONO SBI", aliases=[], cc="in")
    ios = [c for c in result["candidates"] if c["store"] == "app_store"]
    assert {c["id"] for c in ios} == {"com.sbi.YONOBD", "com.sbi.sbiquick.ios"}
    assert all(c["publisher"] == "State Bank of India" for c in ios)


async def test_unreadable_listing_is_a_discovery_error(monkeypatch):
    async def no_listing(pkg, cc):
        return None

    monkeypatch.setattr(discovery, "_play_listing", no_listing)
    with pytest.raises(discovery.DiscoveryError):
        await discovery.discover(PLAY, None, "YONO SBI", aliases=[], cc="in")


# ── brand record: confirmation and the scan merge ─────────────────────────────

@pytest.fixture
def brand_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(brand_store, "_dir", lambda: tmp_path)
    return tmp_path


BRAND_ID = "brd_01HZX0000000000000000000AB"


def _done_record():
    record = brand_store.create(BRAND_ID, {
        "brand_name": "YONO SBI", "play_store_url": PLAY, "official_domains": ["onlinesbi.sbi"],
        "aliases": [], "country": "in",
    })
    return brand_store.set_candidates(record, {
        "play_developer": "State Bank of India",
        "candidates": [
            {"store": "play", "id": "com.sbi.lotusintouch", "name": "YONO SBI", "publisher": "State Bank of India",
             "default": True},
            {"store": "app_store", "id": "com.sbi.YONOBD", "name": "YONO SBI Bangladesh",
             "publisher": "State Bank of India", "default": True},
        ],
    })


def test_confirm_stores_only_selected_candidates_with_audit(brand_dir):
    record = _done_record()
    record = brand_store.confirm(record, [{"store": "play", "id": "com.sbi.lotusintouch"}], "ops@example")
    assert [c["id"] for c in record["confirmed"]] == ["com.sbi.lotusintouch"]
    assert record["confirmed"][0]["confirmed_by"] == "ops@example"
    assert record["confirmed"][0]["confirmed_at"]


def test_confirm_rejects_an_unknown_candidate(brand_dir):
    record = _done_record()
    with pytest.raises(ValueError):
        brand_store.confirm(record, [{"store": "play", "id": "com.not.found"}], "ops@example")


def test_apply_to_request_routes_each_id_to_its_platform(brand_dir):
    record = brand_store.confirm(
        _done_record(),
        [{"store": "play", "id": "com.sbi.lotusintouch"}, {"store": "app_store", "id": "com.sbi.YONOBD"}],
        "ops@example",
    )
    body = ScanRequest(brand_name="YONO SBI", brand_id=BRAND_ID)
    merged = brand_store.apply_to_request(body, record)

    assert merged.android_packages == ["com.sbi.lotusintouch"]
    assert merged.ios_bundles == ["com.sbi.YONOBD"]
    assert merged.official_publishers == ["State Bank of India"]
    assert merged.official_domain == "onlinesbi.sbi"
    # the store-specific ids are on their own platform only
    ids = merged.official_app_ids()
    assert "com.sbi.yonobd" not in ids["android"]
    assert "com.sbi.lotusintouch" not in ids["ios"]


def test_invalid_brand_id_is_not_loaded(brand_dir):
    assert brand_store.load("../etc/passwd") is None
    assert brand_store.load("brd_short") is None


# ── developer page: browser scroll, static fallback ───────────────────────────

async def test_developer_page_read_in_browser_is_complete_without_note(monkeypatch):
    async def scrolled(url, pattern):
        return ["com.a.one", "com.a.two", "com.a.three"], True

    monkeypatch.setattr(discovery, "collect_while_scrolling", scrolled)
    notes: list[str] = []
    packages = await discovery._play_developer_packages("State+Bank+of+India", "in", notes)
    assert packages == ["com.a.one", "com.a.two", "com.a.three"]
    assert notes == []


async def test_developer_page_cut_off_by_round_cap_is_noted_as_incomplete(monkeypatch):
    async def capped(url, pattern):
        return ["com.a.one"], False

    monkeypatch.setattr(discovery, "collect_while_scrolling", capped)
    notes: list[str] = []
    await discovery._play_developer_packages("State+Bank+of+India", "in", notes)
    assert notes == ["the Play developer page may not list every app of the developer"]


async def test_developer_page_falls_back_to_static_page_when_browser_fails(monkeypatch):
    async def broken(url, pattern):
        raise RuntimeError("playwright not installed")

    html = (
        b'<a href="details?id=com.a.one"></a>'
        b'<a href="details?id=com.a.two"></a>'
        b'<a href="details?id=com.a.one"></a>'
    )

    async def static(url, retries):
        return httpx.Response(200, content=html, request=httpx.Request("GET", url))

    monkeypatch.setattr(discovery, "collect_while_scrolling", broken)
    monkeypatch.setattr(discovery, "fetch", static)
    notes: list[str] = []
    packages = await discovery._play_developer_packages("State+Bank+of+India", "in", notes)
    assert packages == ["com.a.one", "com.a.two"]
    assert notes == ["the Play developer page may not list every app of the developer"]
