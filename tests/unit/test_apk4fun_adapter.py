"""APK4Fun parser: reads the rendered result titles, ignores the sidebar, keeps brand matches only."""

from __future__ import annotations

from app.services.keyword_expansion import fold_homoglyphs
from app.sources.apk4fun_adapter import APK4FunAdapter

_PAGE = """
<div class="sidebar-widget"><div class="app">
  <a href="/apk/406095/" title="Yandere Simulator APK v1.3" class="hidden">Yandere Simulator APK v1.3</a>
</div></div>
<div class="gsc-webResult">
  <a class="gs-title" href="https://www.apk4fun.com/apps/in.swiggy.android/"><b>Swiggy</b> - Free Food &amp; Drink App for Android - APK4Fun.com</a>
  <a class="gs-title" href="https://www.apk4fun.com/apk/1176866/"><b>Swiggy</b> 4.107.2 APK (Old Version) - APK4Fun</a>
  <a class="gs-title" href="https://www.apk4fun.com/apps/flikk.social.trending/">Flikk - Free News - APK4Fun</a>
</div>
"""


# the adapter compares folded tokens, so the test folds the brand the same way
def _parse(brand_tokens: set[str]):
    return APK4FunAdapter()._parse(_PAGE, "swiggy", brand_tokens, set())


SWIGGY = {fold_homoglyphs("swiggy")}


def test_only_result_titles_are_read_not_sidebar():
    hits = _parse(SWIGGY)
    assert all("406095" not in h.url for h in hits)
    assert len(hits) == 2


def test_package_id_comes_from_apps_urls_only():
    by_url = {h.url: h for h in _parse(SWIGGY)}
    assert by_url["https://www.apk4fun.com/apps/in.swiggy.android/"].package_id == "in.swiggy.android"
    assert by_url["https://www.apk4fun.com/apk/1176866/"].package_id is None


def test_site_suffix_and_nested_tags_are_cleaned_from_name():
    names = [h.app_name for h in _parse(SWIGGY)]
    assert names[1] == "Swiggy 4.107.2 APK (Old Version)"
    assert "- APK4Fun" not in names[0]


def test_brand_filter_drops_unrelated_results():
    assert all("Flikk" not in h.app_name for h in _parse(SWIGGY))
