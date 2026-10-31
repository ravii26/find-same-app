"""Unit tests for Telegram, Aptoide, and APKMirror adapters."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.models import ScanRequest, ScanContext


def _make_ctx(brand: str = "Petpooja") -> ScanContext:
    req = ScanRequest(
        brand_name=brand,
        android_packages=["com.petpooja.billing"],
        keywords=["petpooja"],
    )
    from app.services.keyword_expansion import expand
    kws = expand(req)
    return ScanContext(
        scan_id="scn_test",
        request=req,
        expanded_keywords=kws,
        max_pages_per_scan=2,
        max_response_bytes=5_000_000,
        depth=1,
    )


# ── Telegram adapter ──────────────────────────────────────────────────────────

_TG_HTML_WITH_MSGS = """
<html><body>
  <article class="tgme_widget_message">
    <div class="tgme_widget_message_text">Get Petpooja APK here! Free download.</div>
    <a class="tgme_widget_message_date" href="https://t.me/petpooja_apk/42">Jun 1</a>
  </article>
  <article class="tgme_widget_message">
    <div class="tgme_widget_message_text">Another message</div>
    <a class="tgme_widget_message_date" href="https://t.me/petpooja_apk/43">Jun 2</a>
  </article>
</body></html>
"""

_TG_HTML_NO_MSGS = "<html><body><p>Channel not found</p></body></html>"


@pytest.mark.asyncio
async def test_telegram_finds_hits():
    from app.sources.telegram_adapter import TelegramAdapter

    resp = MagicMock()
    resp.status_code = 200

    with patch("app.sources.telegram_adapter.fetch", new=AsyncMock(return_value=resp)), \
         patch("app.sources.telegram_adapter.decode_response", return_value=_TG_HTML_WITH_MSGS):
        adapter = TelegramAdapter()
        hits, status = await adapter.search(_make_ctx())

    assert len(hits) > 0
    assert status.status == "ok"


@pytest.mark.asyncio
async def test_telegram_empty_channel():
    from app.sources.telegram_adapter import TelegramAdapter

    resp = MagicMock()
    resp.status_code = 200

    with patch("app.sources.telegram_adapter.fetch", new=AsyncMock(return_value=resp)), \
         patch("app.sources.telegram_adapter.decode_response", return_value=_TG_HTML_NO_MSGS):
        adapter = TelegramAdapter()
        hits, status = await adapter.search(_make_ctx())

    assert hits == []
    assert status.status == "ok"


@pytest.mark.asyncio
async def test_telegram_fetch_error_skipped():
    from app.sources.telegram_adapter import TelegramAdapter
    from app.utils.http_client import FetchError

    with patch("app.sources.telegram_adapter.fetch", new=AsyncMock(side_effect=FetchError("err"))):
        adapter = TelegramAdapter()
        hits, status = await adapter.search(_make_ctx())

    assert hits == []


@pytest.mark.asyncio
async def test_telegram_parse_channel_no_messages():
    from app.sources.telegram_adapter import TelegramAdapter

    adapter = TelegramAdapter()
    hits = adapter._parse_channel(
        "<html><body></body></html>", "test", "https://t.me/s/test", set(), set()
    )
    assert hits == []


@pytest.mark.asyncio
async def test_telegram_404_channel_skipped():
    from app.sources.telegram_adapter import TelegramAdapter

    resp = MagicMock()
    resp.status_code = 404

    with patch("app.sources.telegram_adapter.fetch", new=AsyncMock(return_value=resp)):
        adapter = TelegramAdapter()
        hits, status = await adapter.search(_make_ctx())

    assert hits == []



@pytest.mark.asyncio
async def test_aptoide_no_listing_url_skipped():
    from app.sources.aptoide_adapter import AptoideAdapter

    data = {
        "datalist": {
            "list": [{
                "package": "",
                "name": "Unknown",
                "urls": {},
                "malware": {"rank": "TRUSTED"},
            }]
        }
    }
    resp = MagicMock()
    resp.content = json.dumps(data).encode()

    with patch("app.sources.aptoide_adapter.fetch", new=AsyncMock(return_value=resp)):
        adapter = AptoideAdapter()
        hits, _ = await adapter.search(_make_ctx())

    assert hits == []


# ── APKMirror adapter (parser) ────────────────────────────────────────────────

# Real APKMirror structure: app name lives in the <h5 title="..."> attribute;
# the anchor wraps the icon and has NO text content. Regression guard for the
# bug where reading a.text() yielded "" and silently dropped every result.
_APKMIRROR_HTML = """
<div id="content"><div class="listWidget">
  <div class="appRow">
    <h5 title="Petpooja 25.03.10" class="appRowTitle wrapText">
      <a class="fontBlack" href="/apk/petpooja/petpooja-app/petpooja-25-03-10-release/"><img/></a>
    </h5>
    <div class="byDeveloper">by Petpooja Pvt Ltd</div>
  </div>
  <div class="appRow">
    <h5 title="ACR Phone 0.402" class="appRowTitle">
      <a class="fontBlack" href="/apk/nll-apps/acrphone/acr-phone-0-402-release/"><img/></a>
    </h5>
    <div class="byDeveloper">by NLL</div>
  </div>
</div></div>
"""


def test_apkmirror_parse_reads_name_from_title_attr():
    from app.sources.apkmirror_adapter import APKMirrorAdapter

    adapter = APKMirrorAdapter()
    hits = adapter._parse(_APKMIRROR_HTML, "Petpooja", {"petpooja"}, set())

    # The matching row is extracted (name from <h5 title>), the unrelated
    # "ACR Phone" row is filtered out by the brand-token guard.
    assert len(hits) == 1
    assert hits[0].app_name == "Petpooja 25.03.10"
    assert hits[0].publisher == "by Petpooja Pvt Ltd"
    assert "petpooja" in hits[0].url


def test_apkmirror_parse_empty_without_brand_match():
    from app.sources.apkmirror_adapter import APKMirrorAdapter

    adapter = APKMirrorAdapter()
    # Brand with no matching row → genuinely empty (not a crash / not all-dropped bug)
    hits = adapter._parse(_APKMIRROR_HTML, "PhonePe", {"phonepe"}, set())
    assert hits == []
