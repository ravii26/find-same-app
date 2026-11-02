"""apk4fun app page: version, developer and description are read; the description stops before the permissions list."""

from __future__ import annotations

from app.sources.apk4fun_adapter import app_facts

PAGE = """<html><body><nav>Top Download Apps | Best Rated</nav>
<article><h2>PhonePe UPI App Info</h2>
<ul><li>• Latest Version: 24.04.19.0</li><li>• Updated: May 13, 2024</li><li>• Developer: PhonePe</li></ul>
<p>PhonePe UPI Description: PhonePe is a payments app. Link your bank account.</p>
<p>Permissions for App and reasons
SMS: to verify phone number</p></article></body></html>"""


def test_version_developer_and_short_description():
    assert app_facts(PAGE) == {
        "version": "24.04.19.0",
        "developer": "PhonePe",
        "description": "PhonePe is a payments app. Link your bank account.",
    }


def test_page_without_the_labels_gives_nothing():
    assert app_facts("<html><body><p>nothing here</p></body></html>") == {}
