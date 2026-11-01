"""Medium issues found by the Paytm and parallel-scan tests.

1. The brand's real package re-uploaded under another publisher was capped as a plain "mirror".
2. A sibling app on another store, with a store-shortened publisher name ("... Communications L"), was reported:
   names cut short now match, and such packages are checked on Google Play against the brand's own accounts.
3. With several scans at once, Bing outlasted its time limit and lost everything; it now stops at a budget and
   keeps what it found, covering every market first.
"""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.models import Hit, HitType, HostInfo, ScanContext, ScanRequest
from app.services.allowlist import publisher_matches, split_allowlisted
from app.services.keyword_expansion import expand

PAYTM = ScanRequest(brand_name="Paytm", android_packages=["net.one97.paytm"], official_domain="paytm.com",
                    official_publishers=["Paytm - One97 Communications Ltd."],
                    play_developer_ids=["5032485548806526009"])
PUBLISHERS = frozenset({"paytm one97 communications"})


def _hit(**update) -> Hit:
    base = dict(
        id="1", type=HitType.app_listing, title="x", app_name="x", url="https://en.uptodown.com/android/x",
        host=HostInfo(group="apk_site", name="Uptodown", domain="uptodown.com"),
        discovered_at=datetime.datetime.now(datetime.UTC), relevance_score=60,
    )
    return Hit(**{**base, **update})


# ── 1. A re-upload under another publisher is not a plain mirror ─────────────


def test_a_reupload_under_another_publisher_keeps_its_score():
    from app.services.scoring import _cap_official_copy

    reupload = _hit(package_id="net.one97.paytm", publisher="iReff", relevance_score=74)
    out = _cap_official_copy(reupload, PAYTM, PUBLISHERS)
    assert out.relevance_score == 74
    assert "re-uploaded under another publisher ('iReff')" in out.reasons[-1]


def test_a_copy_with_no_publisher_is_still_capped_as_a_mirror():
    from app.services.scoring import _cap_official_copy

    mirror = _hit(package_id="net.one97.paytm", publisher=None, relevance_score=74)
    assert _cap_official_copy(mirror, PAYTM, PUBLISHERS).relevance_score == 55


# ── 2. Store-shortened publisher names, and siblings verified on Google Play ──


def test_a_publisher_name_cut_short_by_the_store_matches():
    assert publisher_matches("Paytm - One97 Communications L", PUBLISHERS)
    assert publisher_matches("Paytm - One97 Communications Ltd.", PUBLISHERS)


def test_cut_short_matching_is_narrow():
    assert not publisher_matches("Paytm L", frozenset({"paytm"}))           # too few words left
    assert not publisher_matches("Paytm One97 Communications X", PUBLISHERS)  # X is no corporate suffix
    assert not publisher_matches("Totally Legit Apps", PUBLISHERS)
    assert not publisher_matches(None, PUBLISHERS)


async def test_a_sibling_on_another_store_is_cleared_once_play_confirms_the_account():
    from app.services import orchestrator

    sibling = _hit(app_name="Paytm Payments Bank", package_id="com.paytm.bank",
                   publisher="Paytm - One97 Communications L")
    with patch("app.services.play_listing.fetch_account",
               AsyncMock(return_value=("Paytm - One97 Communications Ltd.", "5032485548806526009"))):
        request = await orchestrator._verify_off_play_packages("scn_t", PAYTM, [sibling])
    assert "com.paytm.bank" in request.android_packages
    remaining, allowed = split_allowlisted([sibling], request)
    assert remaining == [] and "publisher" in allowed[0].reason


async def test_a_package_play_shows_under_another_account_is_not_cleared():
    from app.services import orchestrator

    borrowed = _hit(app_name="Paytm Cash", package_id="com.cash.paytm", publisher="Paytm - One97 Communications L")
    with patch("app.services.play_listing.fetch_account", AsyncMock(return_value=("Someone", "999"))):
        request = await orchestrator._verify_off_play_packages("scn_t", PAYTM, [borrowed])
    assert "com.cash.paytm" not in request.android_packages
    assert split_allowlisted([borrowed], request)[0] == [borrowed]


async def test_a_listing_under_another_publisher_is_never_looked_up():
    from app.services import orchestrator

    lookup = AsyncMock(return_value=("Paytm - One97 Communications Ltd.", "5032485548806526009"))
    other = _hit(package_id="com.abtutorial8.paytm", publisher="Techno Future")
    with patch("app.services.play_listing.fetch_account", lookup):
        await orchestrator._verify_off_play_packages("scn_t", PAYTM, [other])
    lookup.assert_not_called()


def test_an_unverified_listing_under_the_brand_publisher_says_so():
    from app.services.scoring import _note_unverified_brand_publisher

    old = _hit(app_name="PayTM", package_id="com.paytm.paywith", publisher="Paytm - One97 Communications L")
    out = _note_unverified_brand_publisher(old, PAYTM, PUBLISHERS)
    assert out.relevance_score == old.relevance_score
    assert "not under the brand's Google Play account" in out.reasons[-1]


# ── 3. Bing keeps what it found when several scans share its rate limit ─────


def _bing_ctx() -> ScanContext:
    req = ScanRequest(brand_name="Paytm")
    return ScanContext(scan_id="t", request=req, expanded_keywords=expand(req))


async def test_bing_stops_at_its_time_budget_and_keeps_its_hits():
    from app.sources import bing_adapter as mod

    clock = iter(range(0, 10_000, 100))  # every query "takes" 100 s
    calls: list[str] = []

    async def _browser(url, parse):
        calls.append(url)
        return [_hit(id=str(len(calls)), url=f"https://x{len(calls)}.example/")]

    settings = MagicMock(source_timeout_seconds=240)
    with patch.object(mod, "hits_from_browser", _browser), \
         patch.object(mod, "scan_countries", return_value=["US", "IN", "GB"]), \
         patch.object(mod.time, "monotonic", lambda: next(clock)), \
         patch("app.config.settings.get_settings", return_value=settings):
        hits, status = await mod.BingAdapter().search(_bing_ctx())

    assert hits, "what was found before the budget is kept"
    assert "time budget reached" in status.error
    assert len(calls) < 9


async def test_bing_covers_every_market_before_the_next_query():
    from app.sources import bing_adapter as mod

    calls: list[str] = []

    async def _browser(url, parse):
        calls.append(url)
        return []

    with patch.object(mod, "hits_from_browser", _browser), \
         patch.object(mod, "scan_countries", return_value=["US", "IN", "GB"]):
        await mod.BingAdapter().search(_bing_ctx())

    first_three = [u.split("cc=")[1] for u in calls[:3]]
    assert first_three == ["US", "IN", "GB"]


async def test_bing_stops_a_query_that_outlasts_the_budget():
    import asyncio

    from app.sources import bing_adapter as mod

    async def _slow(url, parse):
        await asyncio.sleep(30)  # one query stuck waiting for its turn
        return []

    settings = MagicMock(source_timeout_seconds=1)  # budget 0.75 s
    with patch.object(mod, "hits_from_browser", _slow), \
         patch.object(mod, "scan_countries", return_value=["US"]), \
         patch("app.config.settings.get_settings", return_value=settings):
        hits, status = await asyncio.wait_for(mod.BingAdapter().search(_bing_ctx()), 5)

    assert "time budget reached" in status.error
