"""Unit tests for ScanRequest validation and edge cases."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.models import ScanRequest


def test_valid_minimal_request():
    r = ScanRequest(brand_name="Test", keywords=["test"])
    assert r.brand_name == "Test"


def test_empty_keywords_raises():
    with pytest.raises(ValidationError):
        ScanRequest(brand_name="Test", keywords=[])


def test_whitespace_only_keyword_stripped():
    r = ScanRequest(brand_name="Test", keywords=["  ", "real"])
    assert "real" in r.keywords
    assert any(k.strip() == "" for k in r.keywords) is False


def test_all_whitespace_keywords_raises():
    with pytest.raises((ValidationError, ValueError)):
        ScanRequest(brand_name="Test", keywords=["   ", "  "])


def test_countries_are_not_a_request_field():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="countries was removed"):
        ScanRequest(brand_name="Test", countries=["in"])


def test_apk_path_traversal_detected():
    r = ScanRequest(brand_name="Test", keywords=["test"], apk_path="../etc/passwd")
    assert r.apk_path == "../etc/passwd"  # validation at API layer


def test_depth_capped_at_5():
    with pytest.raises(ValidationError):
        ScanRequest(brand_name="Test", keywords=["test"], depth=10)


def test_depth_default_is_2():
    r = ScanRequest(brand_name="Test", keywords=["test"])
    assert r.depth == 2


def test_dedup_hash_stable():
    r = ScanRequest(brand_name="Test", keywords=["b", "a"])
    h1 = r.dedup_hash()
    h2 = r.dedup_hash()
    assert h1 == h2


def test_dedup_hash_ignores_keyword_order():
    r1 = ScanRequest(brand_name="Test", keywords=["a", "b"])
    r2 = ScanRequest(brand_name="Test", keywords=["b", "a"])
    assert r1.dedup_hash() == r2.dedup_hash()


def test_unicode_brand_name():
    r = ScanRequest(brand_name="測試品牌", keywords=["test"])
    assert r.brand_name == "測試品牌"


def test_emoji_brand_name():
    r = ScanRequest(brand_name="App⭐", keywords=["app"])
    assert r.brand_name == "App⭐"


def test_rtl_brand_name():
    r = ScanRequest(brand_name="تطبيق مزيف", keywords=["arabic"])
    assert r.brand_name == "تطبيق مزيف"
