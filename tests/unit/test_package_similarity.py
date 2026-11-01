"""A confirmed Android id of the brand counts for package similarity, whichever of its ids a hit resembles."""

from __future__ import annotations

from app.services.scoring import _score_one


def test_any_confirmed_android_id_counts_for_package_similarity():
    base = dict(
        hit_title="YONO UK", hit_app_name="YONO UK", hit_package_id="com.YONOUKMobileApp", hit_publisher=None,
        hit_url="https://www.apkpure.com/x", hit_host_group="apk_site", brand_name="YONO SBI",
        android_package="com.sbi.lotusintouch", official_domain=None, brand_aliases=("YONO",),
    )
    first_only, _ = _score_one(**base, brand_packages=("com.sbi.lotusintouch",))
    every_id, reasons = _score_one(**base, brand_packages=("com.sbi.lotusintouch", "com.YONOUKMobileApp"))

    assert every_id > first_only
    assert any("closely matches official package" in r for r in reasons)
