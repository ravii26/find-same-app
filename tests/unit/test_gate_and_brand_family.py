"""Found on a live PhonePe scan: the name-only gate hid real fakes, and the brand's own apps topped the results.

1. A repo named "Fake-PhonePe-APK" and a lookalike app ("PhonePay" by "PhonePay Co", com.phonepay.android) carry
   evidence beyond the name and must reach the report; plain name matches stay gated.
2. An app the brand's own store account publishes is the brand's on every store (same package, same publisher),
   and a store's domain ("phonepeappstore.com") is never scored as a lookalike of the brand's domain.
"""

from __future__ import annotations

import datetime

from app.api.models import Hit, HitType, HostInfo, ScanRequest
from app.services.allowlist import split_allowlisted
from app.services.scoring import _gate_name_only, _score_one, min_relevance_for

PHONEPE = ScanRequest(brand_name="PhonePe", android_packages=["com.phonepe.app"], official_domain="phonepe.com",
                      official_publishers=["PhonePe"], play_developer_ids=["PhonePe"])


def _hit(**update) -> Hit:
    base = dict(
        id="1", type=HitType.app_listing, title="x", app_name="x", url="https://aptoide.com/app/x",
        host=HostInfo(group="apk_site", name="Aptoide", domain="aptoide.com"),
        discovered_at=datetime.datetime.now(datetime.UTC), relevance_score=50,
    )
    return Hit(**{**base, **update})


def _repo(name: str) -> Hit:
    return _hit(type=HitType.repo, title=f"someone/{name}", app_name=name, url=f"https://github.com/someone/{name}",
                host=HostInfo(group="apk_site", name="GitHub", domain="github.com"))


def _gated(hit: Hit) -> bool:
    return _gate_name_only(hit, PHONEPE, frozenset({"phonepe"})).relevance_score < min_relevance_for(hit)


# ── 1. Real fakes pass the name-only gate; plain name matches do not ─────────


def test_repos_that_ship_or_fake_the_brand_are_reported():
    for name in ("Fake-PhonePe-APK", "fake-phonepe-apk", "phonepe-sound-box-working-apk",
                 "PhonepeCashback_apk_facebook_freelanceaap", "phonepe-clone"):
        assert not _gated(_repo(name)), name


def test_a_repo_that_only_mentions_the_brand_is_still_gated():
    assert _gated(_repo("phonepe-notes"))


def test_a_lookalike_spelled_through_every_field_is_reported():
    phonepay = _hit(app_name="PhonePay", title="PhonePay", package_id="com.phonepay.android", publisher="PhonePay Co")
    assert not _gated(phonepay)


def test_a_one_field_spelling_coincidence_is_still_gated():
    phonemes = _hit(app_name="Phonemes: IPA & pronuciation", title="Phonemes: IPA & pronuciation",
                    package_id="net.blackenvelope.write.phonemes", publisher="Black Envelope")
    assert _gated(phonemes)


def test_an_old_version_page_of_the_real_app_stays_gated():
    page = _hit(app_name="PhonePe UPI 24.04.05.0 APK (Old Version)", title="PhonePe UPI 24.04.05.0 APK (Old Version)",
                url="https://www.apk4fun.com/history/1/", host=HostInfo(group="apk_site", name="APK4Fun",
                                                                         domain="apk4fun.com"))
    assert _gated(page)


# ── 2. The brand's own apps, verified by its store account, are its own everywhere ──


def _play(pkg: str, name: str, developer_id: str = "PhonePe") -> Hit:
    return _hit(id=f"play-{pkg}", app_name=name, title=name, package_id=pkg, publisher="PhonePe",
                developer_id=developer_id, url=f"https://play.google.com/store/apps/details?id={pkg}",
                host=HostInfo(group="app_store", name="Google Play", domain="play.google.com", is_official_store=True))


def _indus(pkg: str, name: str, publisher: str = "PhonePe") -> Hit:
    return _hit(id=f"indus-{pkg}", app_name=name, title=name, package_id=pkg, publisher=publisher,
                url=f"https://www.phonepeappstore.com/apps/finance/x/{pkg}/",
                host=HostInfo(group="app_store", name="PhonePe Apps", domain="phonepeappstore.com"))


def test_a_sibling_app_verified_on_play_is_allowlisted_on_other_stores():
    hits = [_indus("com.phonepe.app.business", "PhonePe Business"),  # listed before its Play listing
            _play("com.phonepe.app.business", "PhonePe Business UPI")]
    remaining, allowed = split_allowlisted(hits, PHONEPE)
    assert remaining == []
    assert {a.source for a in allowed} == {"PhonePe Apps", "Google Play"}


def test_the_same_package_under_another_publisher_stays_a_finding():
    hits = [_play("com.phonepe.app.business", "PhonePe Business UPI"),
            _indus("com.phonepe.app.business", "PhonePe Business", publisher="Totally Legit Apps")]
    remaining, _ = split_allowlisted(hits, PHONEPE)
    assert [h.publisher for h in remaining] == ["Totally Legit Apps"]


def test_a_package_from_someone_elses_play_account_is_not_borrowed():
    hits = [_play("com.phonepe.app.business", "PhonePe Business UPI", developer_id="SomeoneElse"),
            _indus("com.phonepe.app.business", "PhonePe Business")]
    remaining, _ = split_allowlisted(hits, PHONEPE)
    assert len(remaining) == 2


def _score(url: str, group: str) -> tuple[int, list[str]]:
    return _score_one("PhonePe Business", "PhonePe Business", "com.other.app", "Someone", url, group,
                      "PhonePe", "com.phonepe.app", "phonepe.com")


def test_a_store_domain_is_not_scored_as_a_lookalike_domain():
    _, reasons = _score("https://www.phonepeappstore.com/apps/finance/x/", "app_store")
    assert not any("domain" in r for r in reasons)


def test_a_lookalike_web_domain_is_still_scored():
    _, reasons = _score("https://phonepe-kyc.com/login", "lookalike_domain")
    assert any("domain" in r for r in reasons)
