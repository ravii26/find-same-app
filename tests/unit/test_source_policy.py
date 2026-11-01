"""Per-host rate limits, and the publisher-and-id allowlist rule."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.utils.rate_limiter import RateLimiter


@pytest.mark.parametrize("url, rps", [
    ("https://en.softonic.com/s/paytm", 1),
    ("https://www.apkmirror.com/feed/", 1),
    ("https://appgallery.huawei.com/search", 1),
    ("https://global.app.mi.com/search", 2),
    ("https://www.phonepeappstore.com/search/", 1),
    ("https://www.example-unknown.com/", 2),
])
def test_store_hosts_have_explicit_rate(url, rps):
    from urllib.parse import urlparse

    host = urlparse(url).hostname
    assert RateLimiter(None)._rps(host) == rps


# ── allowlist: an app needs its publisher AND its package id ───────────────────

from app.services.allowlist import allowlist_reason  # noqa: E402


def _app(package_id, publisher, *, url="https://store.example/app", official_store=False):
    return SimpleNamespace(
        package_id=package_id, publisher=publisher, url=url,
        host=SimpleNamespace(group="app_store", is_official_store=official_store),
    )


_OFFICIAL = dict(declared={"com.sister.app"}, official_ids={"android": {"net.one97.paytm"}, "ios": set()}, domains=set(), publishers={"paytm mobile solutions"})


def test_publisher_and_official_package_allowlists():
    hit = _app("net.one97.paytm", "PayTM Mobile Solutions Private Limited")
    assert allowlist_reason(hit, **_OFFICIAL)


def test_publisher_and_declared_sister_package_allowlists():
    hit = _app("com.sister.app", "PayTM Mobile Solutions Private Limited")
    assert allowlist_reason(hit, **_OFFICIAL)


def test_official_store_alone_does_not_allowlist():
    hit = _app("com.paytm.clone", "PayTM Mobile Solutions Private Limited", official_store=True)
    assert allowlist_reason(hit, **_OFFICIAL) is None


def test_package_without_matching_publisher_is_not_allowlisted():
    hit = _app("net.one97.paytm", "rohit raj")
    assert allowlist_reason(hit, **_OFFICIAL) is None


def test_publisher_without_matching_package_is_not_allowlisted():
    hit = _app("com.random.other", "PayTM Mobile Solutions Private Limited")
    assert allowlist_reason(hit, **_OFFICIAL) is None


def test_official_domain_does_not_allowlist_an_app():
    hit = _app("com.random.other", "rohit raj", url="https://www.paytm.com/download")
    assert allowlist_reason(hit, **{**_OFFICIAL, "domains": {"paytm.com"}}) is None


def test_no_declared_publishers_allowlists_nothing():
    hit = _app("net.one97.paytm", "PayTM Mobile Solutions Private Limited")
    assert allowlist_reason(hit, **{**_OFFICIAL, "publishers": set()}) is None


def test_web_hit_on_official_domain_is_still_allowlisted():
    hit = SimpleNamespace(package_id=None, publisher=None, url="https://support.paytm.com/help",
                          host=None)
    assert allowlist_reason(hit, **{**_OFFICIAL, "domains": {"paytm.com"}})


# ── platform-aware ids: a package id is unique per store ──────────────────────

from app.api.models import ScanRequest  # noqa: E402


def _store_app(package_id, publisher, *, store_domain):
    return SimpleNamespace(
        package_id=package_id, publisher=publisher, url=f"https://{store_domain}/app", publisher_ids=[],
        host=SimpleNamespace(group="app_store", is_official_store=True, domain=store_domain),
    )


_SBI = dict(declared=set(), domains=set(), publishers={"state bank of india"})


def test_ios_bundle_is_allowlisted_on_the_app_store():
    hit = _store_app("com.sbi.YONOBD", "State Bank of India", store_domain="apps.apple.com")
    ids = {"android": set(), "ios": {"com.sbi.yonobd"}}
    assert allowlist_reason(hit, official_ids=ids, **_SBI)


def test_ios_bundle_is_not_allowlisted_on_google_play():
    hit = _store_app("com.sbi.YONOBD", "State Bank of India", store_domain="play.google.com")
    ids = {"android": set(), "ios": {"com.sbi.yonobd"}}
    assert allowlist_reason(hit, official_ids=ids, **_SBI) is None


def test_android_id_is_not_allowlisted_on_the_app_store():
    hit = _store_app("com.sbi.lotusintouch", "State Bank of India", store_domain="apps.apple.com")
    ids = {"android": {"com.sbi.lotusintouch"}, "ios": set()}
    assert allowlist_reason(hit, official_ids=ids, **_SBI) is None


def test_extra_android_package_is_allowlisted_on_google_play():
    hit = _store_app("com.YONOMUApp", "State Bank of India", store_domain="play.google.com")
    ids = {"android": {"com.yonomuapp"}, "ios": set()}
    assert allowlist_reason(hit, official_ids=ids, **_SBI)


def test_request_keeps_ids_per_platform():
    req = ScanRequest(
        brand_name="YONO SBI", android_packages=["com.sbi.lotusintouch", "com.sbi.SBIFreedomPlus"],
        ios_bundles=["com.sbi.YONOBD"],
    )
    assert req.official_app_ids() == {
        "android": {"com.sbi.sbifreedomplus", "com.sbi.lotusintouch"},
        "ios": {"com.sbi.yonobd"},
    }
    assert req.android_package == "com.sbi.lotusintouch"  # the first id, read-only


def test_single_id_fields_are_refused_not_dropped():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="android_package was removed"):
        ScanRequest(brand_name="YONO SBI", android_package="com.sbi.lotusintouch")
    with pytest.raises(ValidationError, match="ios_bundle was removed"):
        ScanRequest(brand_name="YONO SBI", ios_bundle="com.sbi.YONOBD")


def test_hit_without_store_domain_is_treated_as_android():
    hit = _store_app("com.sbi.lotusintouch", "State Bank of India", store_domain="play.google.com")
    hit.host = SimpleNamespace(group="app_store", is_official_store=True)  # no domain attribute
    ids = {"android": {"com.sbi.lotusintouch"}, "ios": set()}
    assert allowlist_reason(hit, official_ids=ids, **_SBI)
