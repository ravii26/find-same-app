"""The evidence gate: a store listing with only a name match stays under the reporting threshold."""

from __future__ import annotations

import datetime

import pytest

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


# ── an app that claims to be the brand ────────────────────────────────────────

ZOMATO = ScanRequest(brand_name="Zomato", android_packages=["com.application.zomato"], official_domains=["zomato.com"])


def _zomato_listing(name: str, kind: HitType = HitType.app_listing) -> Hit:
    return _listing(type=kind, title=name, app_name=name, package_id="com.outdarelab.zomato", publisher="ZMTEUROPE, LDA",
                    url="https://zomato-outdarelab.en.uptodown.com/android")


def _zomato_gated(hit: Hit) -> bool:
    return _gate_name_only(hit, ZOMATO, frozenset()).relevance_score < min_relevance_for(hit)


@pytest.mark.parametrize("name", ["Zomato", "Zomato App", "Zomato Official"])
def test_app_named_the_brand_counts_as_evidence(name):
    # an impersonator copies the name; its publisher, package and icon are its own
    assert not _zomato_gated(_zomato_listing(name))


@pytest.mark.parametrize("name", ["Free Coupon Codes for Zomato - Food Ordering", "Zomato Mod", "Zomato KYC Update"])
def test_brand_with_scam_or_mod_wording_counts_as_evidence(name):
    assert not _zomato_gated(_zomato_listing(name))


@pytest.mark.parametrize("name", ["Swiggy Zomato - All In One Food App", "Zomato: Free Food Guide"])
def test_an_app_that_only_contains_the_brand_is_gated(name):
    # "free" is everyday app wording, not bait
    assert _zomato_gated(_zomato_listing(name))


@pytest.mark.parametrize("name", ["Bob World: Super Bro Run Game", "Bob's World : Free Run Game", "Jim Bob World Tour"])
def test_games_that_contain_a_common_word_brand_are_gated(name):
    request = ScanRequest(brand_name="bob World", brand_aliases=["Bank of Baroda", "BOB"])
    hit = _listing(title=name, app_name=name, publisher="OneSoft Global PTE. LTD.")
    assert _gate_name_only(hit, request, frozenset()).relevance_score < min_relevance_for(hit)


def test_a_short_alias_is_not_a_brand_name_claim():
    # "BOB" is also a name and an everyday word: an app named "BOB" by La Poste claims nothing
    request = ScanRequest(brand_name="bob World", brand_aliases=["BOB"])
    hit = _listing(title="BOB", app_name="BOB", publisher="La Poste")
    assert _gate_name_only(hit, request, frozenset()).relevance_score < min_relevance_for(hit)


def test_a_repo_named_the_brand_is_still_gated():
    assert _zomato_gated(_zomato_listing("Zomato", kind=HitType.repo))


def test_request_without_own_apps_domains_or_publishers_is_name_only():
    assert ScanRequest(brand_name="Zomato").is_name_only()
    assert not ZOMATO.is_name_only()
