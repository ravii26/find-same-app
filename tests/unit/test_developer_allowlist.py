"""The brand's store accounts allowlist every app they publish on that store, and nothing anywhere else."""

from __future__ import annotations

import datetime

from app.api.models import FoundVia, Hit, HitType, HostInfo, ScanRequest, SourceMethod
from app.services import orchestrator
from app.services.allowlist import split_allowlisted
from app.services.deduplicator import group_mirrors
from app.services.scoring import _score_one
from app.storage import brand_index

PLAY = HostInfo(group="app_store", name="Google Play", domain="play.google.com", is_official_store=True)
APPLE = HostInfo(group="official_store", name="Apple App Store", domain="apps.apple.com", is_official_store=True)
MIRROR = HostInfo(group="apk_site", name="Aptoide", domain="aptoide.com")


def _hit(pkg: str, host: HostInfo, *, publisher="State Bank of India", developer_id=None, official=False,
         score=0) -> Hit:
    return Hit(
        id=f"{host.domain}/{pkg}", type=HitType.app_listing, url=f"https://{host.domain}/{pkg}",
        app_name=pkg, package_id=pkg, publisher=publisher, developer_id=developer_id, host=host,
        found_via=[FoundVia(source="t", method=SourceMethod.api, query="q")], relevance_score=score,
        is_official_match=official, discovered_at=datetime.datetime.now(datetime.UTC),
    )


def _request(**kw) -> ScanRequest:
    return ScanRequest(brand_name="YONO SBI", android_packages=["com.sbi.lotusintouch"],
                       official_publishers=["State Bank of India"], **kw)


# ── allowlist rule ────────────────────────────────────────────────────────────

def test_unconfirmed_sibling_on_play_is_allowlisted_by_its_developer_account():
    hit = _hit("com.YONODEMobileApp", PLAY, developer_id="State+Bank+of+India")
    remaining, allowed = split_allowlisted([hit], _request(play_developer_ids=["State+Bank+of+India"]))
    assert remaining == []
    assert "account State+Bank+of+India" in allowed[0].reason


def test_copy_with_the_brand_name_but_another_account_is_not_allowlisted():
    hit = _hit("com.YONODEMobileApp", PLAY, developer_id="Someone+Else")
    remaining, _ = split_allowlisted([hit], _request(play_developer_ids=["State+Bank+of+India"]))
    assert remaining == [hit]


def test_developer_id_on_a_third_party_site_does_not_count():
    hit = _hit("com.YONODEMobileApp", MIRROR, developer_id="State+Bank+of+India")
    remaining, _ = split_allowlisted([hit], _request(play_developer_ids=["State+Bank+of+India"]))
    assert remaining == [hit]


def test_a_play_developer_id_does_not_count_on_the_app_store():
    hit = _hit("com.YONODEMobileApp", APPLE, developer_id="State+Bank+of+India")
    remaining, _ = split_allowlisted([hit], _request(play_developer_ids=["State+Bank+of+India"]))
    assert remaining == [hit]


def test_app_store_seller_id_allowlists_the_sellers_apps():
    hit = _hit("com.YONOOMMobileApp", APPLE, developer_id="503784436")
    remaining, allowed = split_allowlisted([hit], _request(app_store_seller_ids=["503784436"]))
    assert remaining == [] and allowed


# ── where the account ids come from ───────────────────────────────────────────

def test_merge_takes_accounts_from_the_brands_own_links_only():
    doc = {"brand_id": "brd_X", "brand_name": "YONO SBI", "allowlisted_apps": [
        {"store": "play", "id": "com.kept", "publisher": "State Bank of India", "developer_id": "Kept+Earlier"},
    ]}
    own = [
        {"store": "play", "id": "com.sbi.lotusintouch", "publisher": "State Bank of India",
         "developer_id": "State+Bank+of+India"},
        {"store": "app_store", "id": "com.sbi.YONOBD", "publisher": "State Bank of India", "developer_id": "503784436"},
    ]
    merged = brand_index.merge_into_request(ScanRequest(brand_id="brd_X"), doc, own)
    assert merged.play_developer_ids == ["State+Bank+of+India"]
    assert merged.app_store_seller_ids == ["503784436"]


def test_seller_of_the_official_app_store_listing_is_learned():
    hits = [
        _hit("com.sbi.YONOBD", APPLE, developer_id="503784436", official=True),
        _hit("com.clone", APPLE, developer_id="999"),
    ]
    assert orchestrator._learn_app_store_sellers(_request(), hits).app_store_seller_ids == ["503784436"]


async def test_only_play_hits_showing_a_brand_publisher_are_looked_up(monkeypatch):
    looked_up = []

    async def fetch_account(pkg, countries=None):
        looked_up.append(pkg)
        return "State Bank of India", "State+Bank+of+India"

    import app.services.play_listing as play_listing
    monkeypatch.setattr(play_listing, "fetch_account", fetch_account)
    hits = [
        _hit("com.YONODEMobileApp", PLAY),
        _hit("com.yono_rummy", PLAY, publisher="Rummy Ltd"),
        _hit("com.YONODEMobileApp", MIRROR),
    ]
    out = await orchestrator._stamp_play_developers("scn_x", _request(play_developer_ids=["State+Bank+of+India"]), hits)
    assert looked_up == ["com.YONODEMobileApp"]
    assert [h.developer_id for h in out] == ["State+Bank+of+India", None, None]


# ── fixes found alongside ─────────────────────────────────────────────────────

def test_lower_cased_official_package_is_not_a_suffix_variation():
    _, reasons = _score_one(
        "x", "YONO SBI UK", "com.yonoukmobileapp", "State Bank of India", "https://aptoide.com/x", "apk_site",
        "YONO SBI", "com.YONOUKMobileApp", None, brand_packages=("com.YONOUKMobileApp",),
    )
    assert not any("suffix variation" in r for r in reasons)


def test_same_id_on_the_app_store_and_a_mirror_is_not_one_app():
    hits = [_hit("com.example.app", APPLE, score=60), _hit("com.example.app", MIRROR, score=50)]
    kept, merged = group_mirrors(hits)
    assert len(kept) == 2 and merged == []


async def test_itunes_android_id_does_not_mark_the_official_listing_and_terms_are_encoded(monkeypatch):
    import json
    from types import SimpleNamespace

    from app.api.models import Keyword, KeywordType, ScanContext
    from app.sources import itunes

    urls = []

    async def fetch(url, **kw):
        urls.append(url)
        item = {"trackName": "Marks&Spencer Clone", "bundleId": "com.marks.android", "artistId": 7,
                "artistName": "Not M&S", "trackViewUrl": "https://apps.apple.com/app/id1"}
        return SimpleNamespace(content=json.dumps({"results": [item]}).encode())

    monkeypatch.setattr(itunes, "fetch", fetch)
    request = ScanRequest(brand_name="Marks&Spencer", android_packages=["com.marks.android"])
    ctx = ScanContext(scan_id="scn_x", request=request,
                      expanded_keywords=[Keyword(term="Marks&Spencer", type=KeywordType.original, reason="brand")])
    hits, _ = await itunes.ItunesAdapter().search(ctx)

    assert hits and not hits[0].is_official_match
    assert hits[0].developer_id == "7"
    assert all("term=Marks%26Spencer" in u for u in urls if "/search?" in u)


async def test_a_hanging_source_is_stopped_and_reported_failed(monkeypatch):
    import asyncio

    from app.api.models import ScanContext, SourceStatus
    from app.config.settings import get_settings
    from app.sources.base import SourceAdapter

    class Hangs(SourceAdapter):
        name = "hangs"
        method = SourceMethod.api

        async def search(self, ctx):
            await asyncio.sleep(60)
            return [], SourceStatus(name=self.name, method=self.method, status="ok")

    monkeypatch.setattr(get_settings(), "source_timeout_seconds", 0.05)
    ctx = ScanContext(scan_id="scn_x", request=_request(), expanded_keywords=[])
    hits, status = await Hangs()._run(ctx)
    assert hits == [] and status.status == "failed" and "timed out" in status.error


# ── a scan stopped by its time limit still allowlists and scores what it collected ─

async def test_timed_out_scan_reports_scored_results_not_raw_hits(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch

    from app.api.models import SourceStatus
    from app.config.settings import get_settings

    sibling = _hit("com.sbi.YONOOM", APPLE, developer_id="503784436")
    sibling = sibling.model_copy(update={"app_name": "YONO SBI Oman"})
    rogue = _hit("com.yono.sbi.rewards", APPLE, publisher="Yono Rewards Team", developer_id="999")
    rogue = rogue.model_copy(update={"app_name": "YONO SBI Rewards KYC"})

    def adapter(name, run):
        a = MagicMock()
        a.name, a.method, a._run = name, SourceMethod.api, run
        return a

    async def answers(ctx):
        return [sibling, rogue], SourceStatus(name="store", method=SourceMethod.api, status="ok", results=2)

    async def hangs(ctx):
        await asyncio.sleep(3600)

    monkeypatch.setattr(get_settings(), "scan_timeout_seconds", 1)
    request = ScanRequest(brand_name="YONO SBI", ios_bundles=["com.sbi.YONOBD"], app_store_seller_ids=["503784436"])
    with patch("app.services.orchestrator.get_all_adapters",
               return_value=[adapter("store", answers), adapter("hangs", hangs)]), \
         patch("app.services.orchestrator.status_store") as ss:
        ss.update_meta_field = ss.set_progress = ss.update_sources = AsyncMock()
        report = await orchestrator.run_scan("scn_t", request, AsyncMock())

    assert "timed out" in report.error
    assert [h.package_id for h in report.results] == ["com.yono.sbi.rewards"]
    assert report.results[0].relevance_score > 0
    assert any(a.package_id == "com.sbi.YONOOM" and "account" in a.reason for a in report.allowlisted)
    assert report.summary.sources_total == 2
    assert {s.name: s.status for s in report.source_statuses} == {"store": "ok", "hangs": "failed"}
    assert not any("not scored" in w for w in report.warnings)


# ── App Store hits are compared with the brand's iOS ids ───────────────────────

async def test_app_store_hit_is_compared_with_ios_bundles():
    from app.services.scoring import score_hit

    request = ScanRequest(brand_name="YONO SBI", android_packages=["com.YONOUKMobileApp"],
                          ios_bundles=["com.sbi.YONOUK"])
    on_apple = await score_hit(_hit("com.sbi.YONOUK2", APPLE, publisher="X"), request)
    on_mirror = await score_hit(_hit("com.sbi.YONOUK2", MIRROR, publisher="X"), request)
    assert any("closely matches official package" in r for r in on_apple.reasons)
    # an Android listing is still compared with the Android ids only
    assert not any("closely matches official package" in r for r in on_mirror.reasons)


async def test_play_lookups_past_the_time_budget_are_dropped_and_the_rest_kept(monkeypatch):
    import asyncio

    import app.services.play_listing as play_listing

    async def fetch_account(pkg, countries=None):
        if pkg == "com.slow":
            await asyncio.sleep(60)
        return "State Bank of India", "State+Bank+of+India"

    monkeypatch.setattr(play_listing, "fetch_account", fetch_account)
    hits = [_hit("com.fast", PLAY), _hit("com.slow", PLAY)]
    out = await orchestrator._stamp_play_developers(
        "scn_x", _request(play_developer_ids=["State+Bank+of+India"]), hits, timeout=0.2)
    assert [h.developer_id for h in out] == ["State+Bank+of+India", None]
