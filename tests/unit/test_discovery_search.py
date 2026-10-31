"""Brand-name search in discovery: it finds apps the listings miss, keeps only brand-named titles, and never pre-selects."""

from __future__ import annotations

from app.services import discovery
from app.services.scoring import _score_one

PLAY_HITS = [
    {"appId": "com.YONODEMobileApp", "title": "YONO SBI Europe", "developer": "State Bank of India", "icon": "i1"},
    {"appId": "com.other.game", "title": "Yono Rummy Store", "developer": "Someone Else", "icon": "i2"},
]
APP_STORE_HITS = [
    {"bundleId": "com.sbi.YONOOM", "trackName": "YONO SBI Oman", "artistName": "State Bank of India"},
    {"bundleId": "com.x.weather", "trackName": "Weather Now", "artistName": "Other"},
]


def _fake_search(monkeypatch, play_error: bool = False):
    async def play_search(term, cc):
        if play_error:
            raise RuntimeError("blocked")
        return PLAY_HITS

    async def itunes(url):
        return {"results": APP_STORE_HITS}

    monkeypatch.setattr(discovery, "_play_search", play_search)
    monkeypatch.setattr(discovery, "_itunes_json", itunes)


async def test_search_adds_brand_named_apps_not_preselected(monkeypatch):
    _fake_search(monkeypatch)
    notes: list[str] = []
    found = await discovery._name_search_candidates(["YONO SBI"], "in", notes)
    by_id = {c["id"]: c for c in found}

    assert set(by_id) == {"com.YONODEMobileApp", "com.sbi.YONOOM"}  # other titles dropped
    assert all(c["default"] is False for c in found)
    assert by_id["com.YONODEMobileApp"]["publisher"] == "State Bank of India"


async def test_alias_lets_a_search_hit_through(monkeypatch):
    _fake_search(monkeypatch)
    found = await discovery._name_search_candidates(["YONO SBI", "YONO"], "in", [])
    assert "com.other.game" in {c["id"] for c in found}  # "Yono Rummy Store" matches the alias


async def test_failed_play_search_is_noted_and_app_store_still_searched(monkeypatch):
    _fake_search(monkeypatch, play_error=True)
    notes: list[str] = []
    found = await discovery._name_search_candidates(["YONO SBI"], "in", notes)

    assert any("Google Play search" in n for n in notes)
    assert {c["store"] for c in found} == {"app_store"}


async def test_listed_app_is_not_replaced_by_a_search_hit(monkeypatch):
    _fake_search(monkeypatch)

    async def listing(pkg, cc):
        return {"title": "YONO SBI: Banking", "developer": "State Bank of India", "developerId": ""}

    async def no_dev(dev_id, cc, notes):
        return []

    monkeypatch.setattr(discovery, "_play_listing", listing)
    monkeypatch.setattr(discovery, "_play_developer_packages", no_dev)
    result = await discovery.discover(
        "https://play.google.com/store/apps/details?id=com.sbi.lotusintouch", None, "YONO SBI", [], "in")
    play = {c["id"]: c for c in result["candidates"] if c["store"] == "play"}

    assert play["com.sbi.lotusintouch"]["default"] is True  # the listed app keeps its own flag
    assert play["com.YONODEMobileApp"]["default"] is False


def test_any_confirmed_android_id_counts_for_package_similarity():
    base = dict(
        hit_title="YONO UK", hit_app_name="YONO UK", hit_package_id="com.YONOUKMobileApp", hit_publisher=None,
        hit_url="https://www.apkpure.com/x", hit_host_group="apk_site", brand_name="YONO SBI",
        android_package="com.sbi.lotusintouch", official_domain=None, brand_aliases=("YONO",),
    )
    first_only, _ = _score_one(**base, brand_packages=("com.sbi.lotusintouch",))
    every_id, reasons = _score_one(**base, brand_packages=("com.sbi.lotusintouch", "com.YONOUKMobileApp"))

    assert every_id > first_only
    assert any("closely matches official package" in r for r in reasons)
