"""APKKit parser: reads the rendered result titles, skips category and developer pages, keeps brand matches only."""

from __future__ import annotations

from app.services.keyword_expansion import fold_homoglyphs
from app.sources.apkkit_adapter import APKKitAdapter

_PAGE = """
<div class="gsc-webResult">
  <a class="gs-image" href="https://apkkit.com/en/fake-paytm-apk">img</a>
  <a class="gs-title" href="https://apkkit.com/en/fake-paytm-apk">Download Fake Paytm APK 10.59.2 for Android &amp; iOS - APKKIT</a>
  <a class="gs-title" href="https://apkkit.com/hi/dev/fake-paytm-inc">fake paytm inc Android Free Apps - APKKIT</a>
  <a class="gs-title" href="https://apkkit.com/en/c/finance">Finance Android Free Apps - APKKIT</a>
  <a class="gs-title" href="https://apkkit.com/zh/flikk-apk">Flikk APK - APKKIT</a>
</div>
"""

PAYTM = {fold_homoglyphs("paytm")}


def _parse():
    return APKKitAdapter()._parse(_PAGE, "paytm", PAYTM, set())


def test_only_app_pages_are_kept():
    urls = [h.url for h in _parse()]
    assert urls == ["https://apkkit.com/en/fake-paytm-apk"]


def test_suffix_is_removed_from_name():
    assert _parse()[0].app_name == "Download Fake Paytm APK 10.59.2 for Android & iOS"


def test_brand_filter_drops_unrelated_app():
    # Flikk is an app page but not the brand, so it never reaches the results
    assert all("Flikk" not in h.app_name for h in _parse())


def test_no_package_id_from_slug():
    assert _parse()[0].package_id is None
