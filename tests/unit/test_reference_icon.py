"""The brand's own icon is the reference for icon comparison even when its listing is allowlisted; code repos get the
same name-only gate as app listings."""

from __future__ import annotations

import datetime

import pytest

from app.api.models import Hit, HitType, HostInfo, ScanRequest
from app.services import scoring
from app.services.scoring import _enrich_phash, _gate_name_only, min_relevance_for

REQUEST = ScanRequest(brand_name="Bob", android_packages=["com.bankofbaroda.mconnect"])
OWN_ICON = "https://play-lh.googleusercontent.com/bob-world-icon"


def _hit(**update) -> Hit:
    base = dict(
        id="1", type=HitType.app_listing, title="Bob Copy", app_name="Bob Copy", package_id="com.copy.app",
        publisher="Someone", url="https://apps.example/copy", icon_url="https://apps.example/copy.png",
        host=HostInfo(group="apk_site", name="Aptoide", domain="aptoide.com"),
        discovered_at=datetime.datetime.now(datetime.UTC), relevance_score=30,
    )
    return Hit(**{**base, **update})


@pytest.fixture
def fake_phash(monkeypatch):
    """Every icon hashes to its url; similarity is 1.0 when the urls match, 0.0 otherwise."""
    from app.utils import phash

    async def compute(url, cache=None):
        return url

    monkeypatch.setattr(phash, "compute_phash", compute)
    monkeypatch.setattr(phash, "phash_similarity", lambda a, b: 1.0 if a == b else 0.0)


async def test_brand_icon_is_the_reference_when_no_official_hit_is_present(fake_phash):
    out = await _enrich_phash([_hit(icon_url=OWN_ICON)], "Bob", reference_icon_url=OWN_ICON)
    assert out[0].icon_similarity == 1.0
    assert any("nearly identical" in r for r in out[0].reasons)


async def test_no_reference_means_no_icon_comparison(fake_phash):
    out = await _enrich_phash([_hit(icon_url=OWN_ICON)], "Bob")
    assert out[0].icon_similarity is None


async def test_an_official_hit_icon_still_wins_over_the_brand_icon(fake_phash):
    official = _hit(id="2", is_official_match=True, icon_url="https://play.example/official.png")
    out = await _enrich_phash([official, _hit(icon_url="https://apps.example/copy.png")], "Bob",
                              reference_icon_url=OWN_ICON)
    copy = next(h for h in out if not h.is_official_match)
    assert copy.icon_similarity == 0.0  # compared with the official hit's icon, not the brand's


async def test_official_hits_are_never_scored_for_icons(fake_phash):
    out = await _enrich_phash([_hit(is_official_match=True, icon_url=OWN_ICON)], "Bob", reference_icon_url=OWN_ICON)
    assert out[0].icon_similarity is None


def test_a_name_only_code_repo_is_gated():
    repo = _hit(type=HitType.repo, title="apk-bob-assistant", app_name="apk-bob-assistant",
                host=HostInfo(group="apk_site", name="GitHub", domain="github.com"), relevance_score=40,
                reasons=["app name 'apk-bob-assistant' resembles brand name", "listed on apk site"])
    gated = _gate_name_only(repo, REQUEST, frozenset())
    assert gated.relevance_score < min_relevance_for(gated)
    assert any("no evidence beyond the name" in r for r in gated.reasons)


def test_a_web_page_is_still_not_gated():
    page = _hit(type=HitType.web_page, relevance_score=40)
    assert _gate_name_only(page, REQUEST, frozenset()).relevance_score == 40
