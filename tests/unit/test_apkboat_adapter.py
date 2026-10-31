"""APKBoat parser: reads the search cards, keeps brand matches only, no package id from slugs."""

from __future__ import annotations

from app.services.keyword_expansion import fold_homoglyphs
from app.sources.apkboat_adapter import APKBoatAdapter

_PAGE = """
<div class="row">
  <div class="col-12"><a class="archive-post" href="https://apkboat.io/nswhatsapp/" title="NSWhatsApp">
    <h3 class="h6">NSWhatsApp</h3></a></div>
  <div class="col-12"><a class="archive-post" href="https://apkboat.io/gt-whatsapp/" title="GT WhatsApp">
    <h3 class="h6">GT WhatsApp</h3></a></div>
  <div class="col-12"><a class="archive-post" href="https://apkboat.io/flikk-apk/" title="Flikk">
    <h3 class="h6">Flikk</h3></a></div>
</div>
"""

WHATSAPP = {fold_homoglyphs("whatsapp")}


def _parse():
    return APKBoatAdapter()._parse(_PAGE, "whatsapp", WHATSAPP, set())


def test_cards_become_hits_with_app_page_urls():
    assert [h.url for h in _parse()] == [
        "https://apkboat.io/nswhatsapp/",
        "https://apkboat.io/gt-whatsapp/",
    ]


def test_brand_filter_drops_unrelated_cards():
    assert all("Flikk" not in h.app_name for h in _parse())


def test_no_package_id_from_slug():
    assert all(h.package_id is None for h in _parse())


def test_no_cards_gives_no_hits():
    assert APKBoatAdapter()._parse("<html><body>No results</body></html>", "paytm", {"paytm"}, set()) == []
