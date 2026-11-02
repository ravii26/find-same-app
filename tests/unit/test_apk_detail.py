"""The app page of an APK site: the facts it lists are read from its text, whatever markup surrounds them."""

from __future__ import annotations

from app.sources.apk_detail import parse_app_page

PAGE = """
<div class="columns appcls"><div class="column"><figure><img class="lazyload" src="/logo/phonepe-ace-apk.webp" data-src="/logo/phonepe-ace-apk.webp" alt="PhonePe ACE APK"></figure></div>
<div class="column">
  <h1 class="title is-4">Download PhonePe ACE APK 3.3.6 for Android 2026</h1>
  <div class="columns is-mobile">
    <div class="column">App By: <br><b><a href="/en/dev/phonepeace">PhonePeACE</a></b></div>
    <div class="column">Version:<br> <b class="has-text-success">3.3.6 </b>for <a href="#"><span>Android</span></a></div>
    <div class="column">Updated On:<br><b>Jul 06, 2026</b></div>
  </div>
  <div class="columns is-mobile">
    <div class="column">Size: <br><b class="has-text-primary"> 47.6 MB</b></div>
    <div class="column">Required Android:<br> <b class="has-text-success">Android 5.1+ </b></div>
    <div class="column">Category: <br><a href="/en/c/action"><b> Action</b></a></div>
  </div>
</div></div>
"""


def test_reads_every_listed_fact():
    assert parse_app_page(PAGE) == {
        "title": "Download PhonePe ACE APK 3.3.6 for Android 2026",
        "icon": "/logo/phonepe-ace-apk.webp",
        "developer": "PhonePeACE",
        "version": "3.3.6",
        "updated_on": "Jul 06, 2026",
        "size": "47.6 MB",
        "required_android": "Android 5.1+",
        "category": "Action",
    }


def test_missing_labels_are_left_out():
    fields = parse_app_page("<html><body><p>Version: 2.0</p></body></html>")
    assert fields == {"version": "2.0"}


def test_empty_page_gives_nothing():
    assert parse_app_page("<html><body></body></html>") == {}
