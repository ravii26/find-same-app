"""Indus app page: the description is the "About the app" text, read whole and capped like the other stores."""

from __future__ import annotations

from app.sources.indus_appstore_adapter import _description_from_detail

PAGE = """<html><body>
<div id="PST1" data-post-type="APP_INFO" data-section-title="About the app"><h3>About the app</h3>
<div class="w-full relative"><div class="font-sans text-[16px] whitespace-pre-line line-clamp-3">PhonePe is a payments app.
Link your bank account on PhonePe &amp; transfer money.

Things you can do:
- Money Transfer
- Recharge Mobile</div><button type="button">Read More</button></div>
<div class="flex flex-wrap gap-[8px]"><p>Finance</p></div></div>
<div id="PST2" data-post-type="APP_USER_RATING"><div class="whitespace-pre-line">not the description</div></div>
</body></html>"""


def test_reads_the_about_the_app_text_whole():
    text = _description_from_detail(PAGE)
    assert text == (
        "PhonePe is a payments app.\nLink your bank account on PhonePe & transfer money.\n\n"
        "Things you can do:\n- Money Transfer\n- Recharge Mobile"
    )


def test_capped_at_1000_characters():
    long_page = PAGE.replace("Recharge Mobile", "x" * 2000)
    assert len(_description_from_detail(long_page)) == 1000


def test_page_without_the_block_gives_none():
    assert _description_from_detail("<html><body><p>nothing</p></body></html>") is None
