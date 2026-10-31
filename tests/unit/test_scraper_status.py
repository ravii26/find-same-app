"""Scraper status helpers and the Softonic / TapTap result parsers.

A scraper that cannot read its target must say so (blocked / failed), not return an empty `ok`.
"""

from __future__ import annotations

import pytest

from app.api.models import SourceMethod
from app.sources import softonic_adapter, taptap_adapter
from app.sources.base import is_network_block, page_problem, says_no_results, scrape_status

_M = SourceMethod.scrape


# ── page_problem ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [403, 406, 412, 429, 503])
def test_error_status_is_a_problem(status):
    assert page_problem(status, "", "") == f"HTTP {status}"


@pytest.mark.parametrize("title", ["Just a moment...", "Sorry! Something went wrong!", "Attention Required!"])
def test_challenge_page_is_a_problem_even_at_200(title):
    assert page_problem(200, title, "") == "block / challenge page"


def test_normal_page_is_not_a_problem():
    assert page_problem(200, "Download whatsapp for Android", "Results for whatsapp") is None


# ── says_no_results / is_network_block ───────────────────────────────────────

def test_says_no_results():
    assert says_no_results("Sorry, no results for your search")
    assert not says_no_results("Results for whatsapp for Android (10000)")


@pytest.mark.parametrize("err", [
    "Page.goto: net::ERR_TIMED_OUT at https://apkpure.com/",
    "Page.goto: Timeout 30000ms exceeded.",
    "net::ERR_NAME_NOT_RESOLVED",
])
def test_network_block_detected(err):
    assert is_network_block(err)


def test_selector_timeout_is_not_a_network_block():
    assert not is_network_block("waiting for selector 'x' failed")


# ── scrape_status ─────────────────────────────────────────────────────────────

def test_status_ok_when_nothing_went_wrong():
    assert scrape_status("x", _M, [], errors=[], blocked=[]).status == "ok"


def test_empty_result_with_a_block_is_blocked_not_ok():
    st = scrape_status("x", _M, [], errors=[], blocked=["HTTP 403 from https://x"])
    assert (st.status, st.results) == ("blocked", 0)
    assert "403" in st.error


def test_empty_result_with_an_error_is_failed():
    assert scrape_status("x", _M, [], errors=["layout changed"], blocked=[]).status == "failed"


def test_blocked_beats_failed_when_both_happened():
    assert scrape_status("x", _M, [], errors=["e"], blocked=["b"]).status == "blocked"


def test_hits_with_a_problem_is_partial():
    st = scrape_status("x", _M, [object()], errors=["second query failed"], blocked=[])  # type: ignore[list-item]
    assert st.status == "partial"


# ── Softonic parser ───────────────────────────────────────────────────────────

def test_softonic_parse_reads_name_from_icon_alt_and_dedupes():
    rows = [
        {"href": "https://whatsapp.en.softonic.com/android", "alt": "Icon of program: WhatsApp", "icon": "i1"},
        {"href": "https://whatsapp.en.softonic.com/android", "alt": "Icon of program: WhatsApp", "icon": "i1"},
        {"href": "https://en.softonic.com/android", "alt": "", "icon": ""},                       # menu link
        {"href": "https://roblox.en.softonic.com/games", "alt": "", "icon": ""},                   # not an app page
    ]
    out = softonic_adapter.parse_results(rows)
    assert [(r["name"], r["url"]) for r in out] == [("WhatsApp", "https://whatsapp.en.softonic.com/android")]


def test_softonic_parse_falls_back_to_slug_when_alt_missing():
    out = softonic_adapter.parse_results([{"href": "https://phone-pe.en.softonic.com/android/", "alt": ""}])
    assert out[0]["name"] == "Phone Pe"


# ── TapTap parser ─────────────────────────────────────────────────────────────

def test_taptap_parse_merges_icon_and_title_anchors_and_ignores_other_hosts():
    rows = [
        {"href": "https://www.taptap.io/app/615", "text": "", "icon": "icon.png"},
        {"href": "https://www.taptap.io/app/615", "text": "Roblox\n\nGlobal", "icon": ""},
        {"href": "https://apps.apple.com/us/app/id6739520838?ct=Tap33789616", "text": "Get", "icon": ""},
        {"href": "https://www.taptap.io/app/999", "text": "", "icon": ""},                        # never titled
    ]
    out = taptap_adapter.parse_results(rows)
    assert out == [{"id": "615", "name": "Roblox", "url": "https://www.taptap.io/app/615", "icon_url": "icon.png"}]
