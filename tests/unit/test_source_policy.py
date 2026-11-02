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


# ── allowlist: a store listing is checked against that store's own metadata ──

from app.services.allowlist import allowlist_reason, store_metadata  # noqa: E402


def _app(package_id, publisher, *, url="https://store.example/app", domain="play.google.com"):
    return SimpleNamespace(
        package_id=package_id, publisher=publisher, url=url,
        host=SimpleNamespace(group="app_store", name="Store", is_official_store=True, domain=domain),
    )


def _store_app(package_id, publisher, *, store_domain):
    return _app(package_id, publisher, domain=store_domain)


PAYTM_PLAY = dict(
    official_ids={"android": set(), "ios": set()}, domains=set(),
    store_meta=store_metadata([("net.one97.paytm", "PayTM Mobile Solutions Private Limited", None)]),
)


def test_play_package_and_publisher_allowlists():
    hit = _app("net.one97.paytm", "PayTM Mobile Solutions Private Limited")
    assert allowlist_reason(hit, **PAYTM_PLAY)


def test_same_package_and_publisher_on_an_apk_site_is_not_allowlisted():
    hit = _app("net.one97.paytm", "PayTM Mobile Solutions Private Limited", domain="apk-site.example")
    assert allowlist_reason(hit, **PAYTM_PLAY) is None


def test_play_hit_with_another_package_is_not_allowlisted():
    hit = _app("com.paytm.clone", "PayTM Mobile Solutions Private Limited")
    assert allowlist_reason(hit, **PAYTM_PLAY) is None


def test_play_package_with_another_publisher_is_not_allowlisted():
    hit = _app("net.one97.paytm", "rohit raj")
    assert allowlist_reason(hit, **PAYTM_PLAY) is None


def test_publisher_with_another_play_package_is_not_allowlisted():
    hit = _app("com.random.other", "PayTM Mobile Solutions Private Limited")
    assert allowlist_reason(hit, **PAYTM_PLAY) is None


def test_no_play_metadata_allowlists_no_play_app():
    hit = _app("net.one97.paytm", "PayTM Mobile Solutions Private Limited")
    assert allowlist_reason(hit, **{**PAYTM_PLAY, "store_meta": {}}) is None


def test_official_domain_does_not_allowlist_an_app():
    hit = _app("com.random.other", "rohit raj", url="https://www.paytm.com/download")
    assert allowlist_reason(hit, **{**PAYTM_PLAY, "domains": {"paytm.com"}}) is None


def test_web_hit_on_official_domain_is_still_allowlisted():
    hit = SimpleNamespace(package_id=None, publisher=None, url="https://support.paytm.com/help", host=None)
    assert allowlist_reason(hit, **{**PAYTM_PLAY, "domains": {"paytm.com"}})


# ── each store is compared with its own metadata only ─────────────────────────

SBI_BOTH = dict(
    official_ids={"android": set(), "ios": set()}, domains=set(),
    store_meta=store_metadata([("com.sbi.lotusintouch", None, None)], [("com.sbi.YONOBD", "State Bank of India", None)]),
)


def test_app_store_bundle_and_seller_allowlists_on_the_app_store():
    hit = _store_app("com.sbi.YONOBD", "State Bank of India", store_domain="apps.apple.com")
    assert allowlist_reason(hit, **SBI_BOTH)


def test_app_store_bundle_is_not_allowlisted_on_google_play():
    hit = _store_app("com.sbi.YONOBD", "State Bank of India", store_domain="play.google.com")
    assert allowlist_reason(hit, **SBI_BOTH) is None


def test_play_package_is_not_allowlisted_on_the_app_store():
    hit = _store_app("com.sbi.lotusintouch", "State Bank of India", store_domain="apps.apple.com")
    assert allowlist_reason(hit, **SBI_BOTH) is None


def test_app_store_seller_does_not_clear_a_play_listing():
    # The Play listing has no publisher metadata, so the App Store's seller name is not used for it
    hit = _store_app("com.sbi.lotusintouch", "State Bank of India", store_domain="play.google.com")
    assert allowlist_reason(hit, **SBI_BOTH) is None


from app.api.models import ScanRequest  # noqa: E402


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


