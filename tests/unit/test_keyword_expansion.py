"""Unit tests for keyword expansion."""

from __future__ import annotations

import pytest

from app.api.models import KeywordType, ScanRequest
from app.services.keyword_expansion import (
    expand,
    fold_homoglyphs,
    _homoglyph_variants,
    _phonetic_variants,
    _spacing_variants,
    _typo_variants,
)


def _make_request(**kwargs) -> ScanRequest:
    base = {
        "brand_name": "Petpooja",
        "android_packages": ["com.petpooja.billing"],
        "keywords": ["petpooja", "billing"],
    }
    base.update(kwargs)
    return ScanRequest(**base)


def test_expand_contains_original():
    kws = expand(_make_request())
    terms = {k.term.lower() for k in kws}
    assert "petpooja" in terms


def test_expand_contains_lure():
    kws = expand(_make_request())
    types = {k.type for k in kws}
    assert KeywordType.lure in types


def test_expand_contains_typo():
    kws = expand(_make_request())
    types = {k.type for k in kws}
    assert KeywordType.typo in types


def test_expand_contains_homoglyph():
    kws = expand(_make_request())
    types = {k.type for k in kws}
    assert KeywordType.homoglyph in types


def test_expand_contains_package_guess():
    kws = expand(_make_request())
    types = {k.type for k in kws}
    assert KeywordType.package_guess in types


def test_expand_deduplicated():
    kws = expand(_make_request())
    terms = [k.term.lower() for k in kws]
    assert len(terms) == len(set(terms)), "Duplicate terms found"


def test_expand_respects_max_keywords(monkeypatch):
    monkeypatch.setattr("app.services.keyword_expansion.get_settings", lambda: type("S", (), {"max_keywords": 5})())
    kws = expand(_make_request())
    assert len(kws) <= 5


def test_expand_empty_keywords_raises():
    with pytest.raises(Exception):
        ScanRequest(brand_name="X", keywords=[])


def test_expand_unicode_brand():
    r = _make_request(brand_name="Café App", keywords=["café"])
    kws = expand(r)
    assert len(kws) > 0


def test_expand_emoji_in_brand():
    r = _make_request(brand_name="Star⭐App", keywords=["star"])
    kws = expand(r)
    assert len(kws) > 0


def test_spacing_variants_multi_word():
    variants = _spacing_variants("shark striker")
    assert "sharkstriker" in variants
    assert "shark-striker" in variants


def test_spacing_variants_single_word():
    variants = _spacing_variants("petpooja")
    assert variants == []


def test_typo_variants_not_empty():
    variants = _typo_variants("petpooja")
    assert len(variants) > 0


def test_typo_variants_no_original():
    original = "petpooja"
    variants = _typo_variants(original)
    assert original not in variants


def test_homoglyph_variants_not_empty():
    variants = _homoglyph_variants("petpooja")
    assert len(variants) > 0


def test_phonetic_variants_ph_f():
    variants = _phonetic_variants("pharma")
    assert "farma" in variants


def test_fold_homoglyphs_cyrillic():
    # Cyrillic 'а' should fold to ASCII 'a'
    assert fold_homoglyphs("реtpoоja") != "реtpoоja"


def test_expand_package_tokens():
    # Use keywords that don't include the package tokens so they're not deduplicated
    r = _make_request(android_packages=["com.petpooja.mobileapp"], keywords=["petpooja"])
    kws = expand(r)
    package_terms = [k.term for k in kws if k.type == KeywordType.package]
    assert len(package_terms) > 0, "No package tokens generated"
    # "mobileapp" should appear as a package token
    all_terms = {k.term.lower() for k in kws}
    assert "mobileapp" in all_terms, "'mobileapp' package token should appear"


def test_expand_rtl_keywords():
    r = _make_request(brand_name="تطبيق", keywords=["arabic"])
    kws = expand(r)
    assert len(kws) > 0


def test_expand_very_long_keyword():
    long_kw = "a" * 190
    r = _make_request(keywords=[long_kw, "short"])
    kws = expand(r)
    assert len(kws) > 0
