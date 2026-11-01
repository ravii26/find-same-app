"""The evidence gate: a store listing with only a name match stays under the reporting threshold."""

from __future__ import annotations

import datetime

from app.api.models import Hit, HitType, HostInfo, ScanRequest
from app.services.scoring import _gate_name_only, min_relevance_for

REQUEST = ScanRequest(
    brand_name="Bob", android_packages=["com.bankofbaroda.mconnect"],
    official_domain="bankofbaroda.bank.in", brand_aliases=["bob World"],
)
PUBLISHERS = {"bank of baroda"}


def _listing(**update) -> Hit:
    base = dict(
        id="1", type=HitType.app_listing, title="Bob HR", app_name="Bob HR", package_id="com.hibob",
        publisher="Hi Bob Ltd", url="https://play.google.com/store/apps/details?id=com.hibob",
        host=HostInfo(group="official_store", name="Google Play", domain="play.google.com", is_official_store=True),
        discovered_at=datetime.datetime.now(datetime.UTC), relevance_score=65,
    )
    return Hit(**{**base, **update})


def _gated(hit: Hit, publishers=frozenset()) -> bool:
    return _gate_name_only(hit, REQUEST, publishers).relevance_score < min_relevance_for(hit)


def test_name_only_listing_is_held_under_the_threshold():
    hit = _gate_name_only(_listing(), REQUEST, frozenset())
    assert hit.relevance_score < min_relevance_for(hit)
    assert any("no evidence beyond the name" in r for r in hit.reasons)


def test_matching_publisher_counts_as_evidence():
    assert not _gated(_listing(publisher="Bank of Baroda"), PUBLISHERS)


def test_close_icon_counts_as_evidence():
    assert not _gated(_listing(icon_similarity=0.85))


def test_icon_below_the_match_level_does_not_count():
    assert _gated(_listing(icon_similarity=0.47))


def test_app_id_close_to_an_official_id_counts_as_evidence():
    assert not _gated(_listing(package_id="com.bankofbaroda.mconnect.copy", publisher="Someone"))


def test_description_repeating_the_name_is_not_evidence():
    # "Bob HR" describes itself as "Bob's mobile app": the brand word alone must not pass the gate
    assert _gated(_listing(description="Bob's mobile app provides a seamless HR experience"))


def test_official_domain_counts_as_evidence():
    assert not _gated(_listing(url="https://bankofbaroda.bank.in/download"))


def test_web_pages_are_not_gated():
    web = _listing(type=HitType.web_page, host=HostInfo(group="web_search", name="Bing", domain="bing.com"))
    assert not _gated(web)


def test_official_match_is_never_gated():
    assert not _gated(_listing(is_official_match=True))
