"""Unit tests for scoring — deterministic given fixed weights."""

from __future__ import annotations

import datetime
import pytest

from app.api.models import (
    EvidenceRecord,
    FoundVia,
    Hit,
    HitType,
    HostInfo,
    SourceMethod,
)
from app.services.scoring import _score_one, apply_page_assessment, min_relevance, ruleset_version


def _make_hit(**kwargs) -> Hit:
    base = dict(
        id="abc123",
        type=HitType.app_listing,
        title="Petpooja Fake",
        app_name="Petpooja Fake",
        package_id="com.petpooja.fake",
        url="https://apkpure.com/petpooja-fake/com.petpooja.fake",
        host=HostInfo(group="apk_site", name="APKPure", domain="apkpure.com"),
        found_via=[FoundVia(source="brave", method=SourceMethod.api, query="petpooja")],
        relevance_score=0,
        discovered_at=datetime.datetime.now(datetime.UTC),
    )
    base.update(kwargs)
    return Hit(**base)


def test_score_similar_name_high():
    score, reasons = _score_one(
        "Petpooja Fake", "Petpooja Fake", "com.petpooja.fake", None,
        "https://apkpure.com/x", "apk_site",
        "Petpooja", "com.petpooja.billing", "petpooja.com",
    )
    assert score > 40, f"Expected score > 40, got {score}"


def test_score_different_name_low():
    score, reasons = _score_one(
        "Completely Different App", "Completely Different App", "com.other.thing", None,
        "https://example.com", None,
        "Petpooja", "com.petpooja.billing", "petpooja.com",
    )
    assert score < 40, f"Expected score < 40, got {score}"


def test_score_deterministic():
    args = (
        "Petpooja", "Petpooja", "com.petpooja.app", None,
        "https://apksite.com/x", "apk_site",
        "Petpooja", "com.petpooja.billing", "petpooja.com",
    )
    s1, _ = _score_one(*args)
    s2, _ = _score_one(*args)
    assert s1 == s2, "Scoring must be deterministic"


def test_score_capped_at_100():
    score, _ = _score_one(
        "Petpooja", "Petpooja", "com.petpooja.billing", "Petpooja Inc",
        "https://petpooja-apk.com/x", "apk_site",
        "Petpooja", "com.petpooja.billing", "petpooja.com",
    )
    assert score <= 100


def test_score_minimum_zero():
    score, _ = _score_one(
        None, None, None, None,
        "https://example.com", None,
        "Petpooja", None, None,
    )
    assert score >= 0


def test_score_reasons_nonempty_for_match():
    _, reasons = _score_one(
        "Petpooja Fake", "Petpooja Fake", None, None,
        "https://apksite.com/x", "apk_site",
        "Petpooja", None, "petpooja.com",
    )
    assert len(reasons) > 0


def test_ruleset_version_is_string():
    v = ruleset_version()
    assert isinstance(v, str) and len(v) > 0


def test_score_domain_tld_swap():
    score, reasons = _score_one(
        None, None, None, None,
        "https://petpooja.net/app", None,
        "Petpooja", None, "petpooja.com",
    )
    reason_text = " ".join(reasons).lower()
    assert "tld" in reason_text or score > 0


def test_score_lure_keyword_in_title():
    score, reasons = _score_one(
        "Petpooja Mod APK", "Petpooja Mod APK", None, None,
        "https://apksite.com/x", "apk_site",
        "Petpooja", None, None,
    )
    assert any("lure" in r.lower() or "mod" in r.lower() for r in reasons)


def _lookalike(assessment: str | None, **kwargs) -> Hit:
    evidence = EvidenceRecord(
        captured_at=datetime.datetime.now(datetime.UTC), assessment=assessment,
    ) if assessment else None
    return _make_hit(
        type=HitType.web_page,
        host=HostInfo(group="lookalike_domain", name="Lookalike domain", domain="petpooja.co"),
        url="https://petpooja.co/", relevance_score=55, evidence=evidence, **kwargs,
    )


@pytest.mark.parametrize("assessment", [None, "parked", "blank", "neutral"])
def test_lookalike_domain_serving_nothing_suspicious_falls_below_threshold(assessment):
    hit = apply_page_assessment(_lookalike(assessment))
    assert hit.relevance_score < min_relevance()
    assert "nothing suspicious" in hit.reasons[-1]


def test_unprobed_lookalike_domain_says_page_could_not_be_checked():
    assert "could not be checked" in apply_page_assessment(_lookalike(None)).reasons[-1]


def test_page_assessment_is_idempotent():
    once = apply_page_assessment(_lookalike(None))
    assert apply_page_assessment(once) == once


def test_page_assessment_ignores_other_hit_types_without_a_page_check():
    hit = _make_hit(relevance_score=55)
    assert apply_page_assessment(hit).relevance_score == 55


def test_lookalike_domain_mentioning_the_brand_keeps_its_score():
    hit = apply_page_assessment(_lookalike("mentions_brand"))
    assert hit.relevance_score == 55


def test_lookalike_domain_with_brand_bait_is_boosted():
    hit = apply_page_assessment(_lookalike("brand_bait"))
    assert hit.relevance_score > 55
