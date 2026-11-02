"""apkkit: the search card's link title is kept as the title, and the app's own page gives the name, developer and version."""

from __future__ import annotations

import datetime

import httpx
import pytest

from app.api.models import FoundVia, Hit, HostInfo
from app.sources import apkkit_adapter
from app.sources.apkkit_adapter import APKKitAdapter, _app_name
from app.utils.http_client import FetchError

PAGE = """<html><body><div class="columns appcls"><div class="column"><figure>
<img class="lazyload" src="/logo/phonepe-ace-apk.webp" data-src="/logo/phonepe-ace-apk.webp" alt="PhonePe ACE APK"></figure></div>
<div class="column"><h1 class="title is-4">Download PhonePe ACE APK 3.3.6 for Android 2026</h1>
<div>App By: <br><b><a href="/en/dev/phonepeace">PhonePeACE</a></b></div>
<div>Version:<br><b>3.3.6 </b>for <a href="#">Android</a></div>
<div>Updated On:<br><b>Jul 06, 2026</b></div>
<div>Size: <br><b> 47.6 MB</b></div>
<div>Required Android:<br><b>Android 5.1+ </b></div>
<div>Category: <br><a href="/en/c/action"><b> Action</b></a></div>
</div></div></body></html>"""


def _hit(url: str, title: str) -> Hit:
    return Hit(
        id="h1", type="app_listing", url=url, app_name=title,
        host=HostInfo(group="apk_site", name="APKKit", domain="apkkit.com"),
        found_via=[FoundVia(source="apkkit", method="scrape", query="PhonePe")],
        relevance_score=0, discovered_at=datetime.datetime.now(datetime.UTC),
    )


@pytest.mark.parametrize("title, name", [
    ("Download PhonePe ACE APK 3.3.6 for Android & iOS", "PhonePe ACE"),
    ("Download PhonePe ACE APK 3.3.6 for Android 2026", "PhonePe ACE"),
    ("PhonePe Business for Android", "PhonePe Business"),
    ("PhonePe", "PhonePe"),
])
def test_app_name_is_read_from_the_title(title, name):
    assert _app_name(title) == name


async def test_app_page_fills_name_publisher_and_version(monkeypatch):
    async def fake_fetch(url, **_):
        return httpx.Response(200, text=PAGE, headers={"content-type": "text/html; charset=utf-8"})

    monkeypatch.setattr(apkkit_adapter, "fetch", fake_fetch)
    hit = await APKKitAdapter()._with_app_page(_hit("https://apkkit.com/en/phonepe-ace-apk",
                                                    "Download PhonePe ACE APK 3.3.6 for Android & iOS"))
    assert hit.title == "Download PhonePe ACE APK 3.3.6 for Android 2026"
    assert hit.app_name == "PhonePe ACE"
    assert hit.icon_url == "https://apkkit.com/logo/phonepe-ace-apk.webp"
    assert hit.publisher == "PhonePeACE"
    assert hit.version == "3.3.6"


async def test_unreadable_app_page_keeps_the_search_card(monkeypatch):
    async def failing_fetch(url, **_):
        raise FetchError("timeout")

    monkeypatch.setattr(apkkit_adapter, "fetch", failing_fetch)
    original = _hit("https://apkkit.com/en/x", "Download X APK for Android")
    assert await APKKitAdapter()._with_app_page(original) == original
