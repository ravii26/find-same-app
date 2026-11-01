"""A source that cannot be reached must never look like a clean "no results", and must not cost the whole scan.

Covers the three problems found by a live scan:
  1. Samsung and Uptodown reported `ok, 0 results` when no page loaded (Samsung now reads the store's search API).
  2. Unreachable hosts were retried on every query until the scan hit its time limit.
  3. A scan that stopped early reported its hits unscored and left unfinished sources out of the report
     (now handled by the orchestrator's _rank_unfinished and the per-source limit in SourceAdapter._run).
"""

from __future__ import annotations

import asyncio
import datetime
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.api.models import FoundVia, Hit, HitType, HostInfo, ScanContext, ScanRequest, SourceMethod, SourceStatus
from app.services.keyword_expansion import expand
from app.sources.base import SourceAdapter, note_page_error
from app.utils.http_client import BlockedError, FetchError

# ── 1. Browser scrapers report blocked / failed, not ok ───────────────────────


class _FakePage:
    def __init__(self, goto):
        self._goto = goto

    async def goto(self, url, **kwargs):
        return await self._goto(url)

    async def title(self):
        return ""

    async def close(self):
        pass


def _fake_browser_context(goto):
    """browser_context() stand-in whose pages answer goto() with `goto`."""
    browser_ctx = MagicMock()
    browser_ctx.new_page = AsyncMock(side_effect=lambda: _FakePage(goto))

    @asynccontextmanager
    async def _cm():
        yield browser_ctx

    return _cm


def _ctx(**request) -> ScanContext:
    req = ScanRequest(brand_name="PhonePe", **request)
    return ScanContext(scan_id="scn_test", request=req, expanded_keywords=expand(req))


async def _unreachable(url):
    raise Exception(f"Page.goto: net::ERR_NAME_NOT_RESOLVED at {url}")


async def _layout_bug(url):
    raise RuntimeError("selector engine crashed")


async def _http_403(url):
    resp = MagicMock()
    resp.status = 403
    return resp


@pytest.mark.parametrize("module, cls", [
    ("app.sources.uptodown_adapter", "UptodownAdapter"),
])
@pytest.mark.parametrize("goto, expected", [
    (_unreachable, "blocked"),
    (_http_403, "blocked"),
    (_layout_bug, "failed"),
])
async def test_browser_scraper_that_loads_no_page_is_not_ok(module, cls, goto, expected):
    import importlib
    mod = importlib.import_module(module)
    with patch.object(mod, "browser_context", _fake_browser_context(goto)), \
         patch.object(mod, "throttle", AsyncMock()):
        hits, status = await getattr(mod, cls)().search(_ctx())
    assert hits == []
    assert status.status == expected
    assert status.error


_SAMSUNG_XML = (
    '<?xml version="1.0" encoding="UTF-8"?><SamsungProtocol version="7.6"><response id="2040" returnCode="0" '
    'startNum="1" endNum="20" endOfList="0"><errorInfo><errorString errorCode="0"/></errorInfo>'
    '<list><value name="productName">PhonePe UPI, Payment, Recharge</value><value name="GUID">com.phonepe.app</value>'
    '<value name="sellerName">PhonePe</value><value name="productImgUrl">https://img/x.png</value></list>'
    '<list><value name="productName">PhonePay</value><value name="GUID">com.phonepay.android</value>'
    '<value name="sellerName">PhonePay Co</value></list></response></SamsungProtocol>'
)


def test_samsung_parses_the_stores_search_reply():
    from app.sources.samsung_adapter import parse_results
    apps = parse_results(_SAMSUNG_XML)
    assert [a["package"] for a in apps] == ["com.phonepe.app", "com.phonepay.android"]
    assert apps[0]["publisher"] == "PhonePe" and apps[1]["icon"] is None


@pytest.mark.parametrize("reply, expected", [
    (BlockedError("HTTP 403 from https://galaxystore.samsung.com"), "blocked"),
    (FetchError("Could not connect to galaxystore.samsung.com"), "failed"),
    ('<SamsungProtocol><response returnCode="1"/></SamsungProtocol>', "failed"),
    ("<html>not xml", "failed"),
])
async def test_samsung_that_gets_no_answer_is_not_ok(reply, expected):
    from app.sources import samsung_adapter as mod

    async def _fetch(url, **kw):
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, text=reply, headers={"content-type": "application/xml; charset=utf-8"})

    with patch.object(mod, "fetch", _fetch):
        hits, status = await mod.SamsungAdapter().search(_ctx())
    assert hits == []
    assert status.status == expected and status.error


async def test_samsung_reports_its_search_results():
    from app.sources import samsung_adapter as mod

    async def _fetch(url, **kw):
        return httpx.Response(200, text=_SAMSUNG_XML, headers={"content-type": "application/xml; charset=utf-8"})

    with patch.object(mod, "fetch", _fetch):
        hits, status = await mod.SamsungAdapter().search(_ctx())
    assert status.status == "ok"
    assert {h.package_id for h in hits} == {"com.phonepe.app", "com.phonepay.android"}
    assert all(h.host.is_official_store for h in hits)


def test_note_page_error_sorts_network_blocks_from_other_errors():
    errors, blocked = [], []
    note_page_error(Exception("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://x"), errors, blocked)
    note_page_error(ValueError("bad selector"), errors, blocked)
    assert len(blocked) == 1 and "ERR_TUNNEL" in blocked[0]
    assert errors == ["ValueError: bad selector"]


async def test_lookalike_domains_fail_when_dns_does_not_work():
    from app.sources import lookalike_domain_adapter as mod
    with patch.object(mod, "_resolves", AsyncMock(return_value=False)):
        hits, status = await mod.LookalikeDomainAdapter().search(_ctx(official_domain="phonepe.com"))
    assert hits == []
    assert status.status == "failed"
    assert "does not resolve" in status.error


async def test_lookalike_domain_that_offers_an_apk_is_reported():
    """The one case this source reports. It used to crash building the hit (discovered_at passed twice)."""
    from app.api.models import EvidenceRecord
    from app.sources import lookalike_domain_adapter as mod
    check = EvidenceRecord(captured_at=datetime.datetime.now(datetime.UTC), http_status=200, assessment="brand_bait")
    probe = AsyncMock(return_value=("PhonePe APK download", "phonepe-apk.com", "https://phonepe-apk.com/phonepe.apk", check))
    with patch.object(mod, "_resolves", AsyncMock(return_value=True)), patch.object(mod, "_probe", probe):
        hits, status = await mod.LookalikeDomainAdapter().search(_ctx(official_domain="phonepe.com"))
    assert status.status == "ok"
    assert hits and status.results == len(hits)
    assert all(h.apk_url == "https://phonepe-apk.com/phonepe.apk" and h.discovered_at for h in hits)


# ── 2. An unreachable host is not retried for the rest of the scan ────────────


def _refusing_client(calls: list[str]):
    @asynccontextmanager
    async def _stream(method, url, **kwargs):
        calls.append(url)
        raise httpx.ConnectError("[Errno -2] Name or service not known")
        yield  # pragma: no cover

    client = MagicMock()
    client.stream = _stream
    return client


async def test_connect_error_is_retried_once_not_http_retries_times():
    from app.utils.http_client import FetchError, fetch
    calls: list[str] = []
    with patch("app.utils.http_client.check_url"), \
         patch("app.utils.http_client.throttle", AsyncMock()), \
         patch("app.utils.http_client.get_client", AsyncMock(return_value=_refusing_client(calls))):
        with pytest.raises(FetchError, match="Could not connect"):
            await fetch("https://down.example/a", retries=4, backoff_base=0.0)
    assert len(calls) == 2  # first try + one retry


async def test_host_is_skipped_after_repeated_connect_failures():
    from app.utils.http_client import FetchError, fetch
    calls: list[str] = []
    with patch("app.utils.http_client.check_url"), \
         patch("app.utils.http_client.throttle", AsyncMock()), \
         patch("app.utils.http_client.get_client", AsyncMock(return_value=_refusing_client(calls))):
        for path in ("a", "b"):
            with pytest.raises(FetchError):
                await fetch(f"https://down.example/{path}", backoff_base=0.0)
        tried = len(calls)
        with pytest.raises(FetchError, match="not tried again"):
            await fetch("https://down.example/c", backoff_base=0.0)
        assert len(calls) == tried  # no request was made for the third fetch

        # Another host is unaffected
        with pytest.raises(FetchError, match="Could not connect"):
            await fetch("https://other.example/a", backoff_base=0.0)


async def test_a_success_clears_the_unreachable_mark():
    from app.utils import http_client
    from app.utils.http_client import fetch
    http_client._mark_unreachable("flaky.example", httpx.ConnectError("x"))

    ok = MagicMock(status_code=200, headers={}, is_redirect=False, request=None)
    ok.raise_for_status = MagicMock()

    async def aiter_bytes(chunk_size=8192):
        yield b"ok"

    ok.aiter_bytes = aiter_bytes

    @asynccontextmanager
    async def _stream(method, url, **kwargs):
        yield ok

    client = MagicMock()
    client.stream = _stream
    with patch("app.utils.http_client.check_url"), \
         patch("app.utils.http_client.throttle", AsyncMock()), \
         patch("app.utils.http_client.get_client", AsyncMock(return_value=client)):
        await fetch("https://flaky.example/")
    assert "flaky.example" not in http_client._unreachable_hosts()


# ── 3. Slow sources are cut off; what was found is still scored and reported ──


def _hit(title: str, url: str) -> Hit:
    return Hit(
        id=url[-12:], type=HitType.app_listing, title=title, app_name=title, package_id="com.phonepe.rewards",
        url=url, host=HostInfo(group="apk_site", name="Aptoide", domain="aptoide.com"),
        found_via=[FoundVia(source="fast", method=SourceMethod.api, query="PhonePe")],
        relevance_score=0, discovered_at=datetime.datetime.now(datetime.timezone.utc),
    )


class _Adapter(SourceAdapter):
    """A real adapter (so SourceAdapter._run and its time limit apply) that answers after `delay` seconds."""
    method = SourceMethod.api
    host_group = "apk_site"

    def __init__(self, name: str, hits: list[Hit], delay: float = 0.0):
        self.name, self._hits, self._delay = name, hits, delay

    async def search(self, ctx):
        await asyncio.sleep(self._delay)
        return self._hits, SourceStatus(name=self.name, method=self.method, status="ok", results=len(self._hits))


def _status_store():
    ss = MagicMock()
    for fn in ("update_meta_field", "set_progress", "update_sources"):
        setattr(ss, fn, AsyncMock())
    return ss


async def _scan(adapters, settings, request=None):
    from app.services.orchestrator import run_scan
    request = request or ScanRequest(brand_name="PhonePe", official_publishers=["PhonePe Private Limited"])
    with patch("app.services.orchestrator.get_all_adapters", return_value=adapters), \
         patch("app.services.orchestrator.status_store", _status_store()), \
         patch("app.services.orchestrator.get_settings", return_value=settings), \
         patch("app.config.settings.get_settings", return_value=settings):
        return await run_scan("scn_test", request, AsyncMock())


def _fake_hit():
    return _hit("PhonePe Rewards Mod APK", "https://aptoide.com/app/rewards")


async def test_slow_source_is_stopped_at_its_own_time_limit():
    from app.config.settings import Settings
    report = await _scan([_Adapter("fast", [_fake_hit()]), _Adapter("slow", [], delay=30)],
                         Settings(source_timeout_seconds=1))

    statuses = {s.name: s for s in report.source_statuses}
    assert statuses["slow"].status == "failed"
    assert "timed out after 1s" in statuses["slow"].error
    assert report.error is None  # the scan itself finished normally
    assert report.results and report.results[0].relevance_score > 0


async def test_scan_stopped_early_still_scores_and_lists_every_source():
    from app.config.settings import Settings
    settings = Settings(source_timeout_seconds=300)
    # The whole scan stops (as at SCAN_TIMEOUT_SECONDS) while the slow source is still running
    with patch.object(type(settings), "scan_ceiling_seconds", lambda self: 1):
        report = await _scan([_Adapter("fast", [_fake_hit()]), _Adapter("slow", [], delay=60)], settings)

    assert report.error and "timed out" in report.error
    statuses = {s.name: s for s in report.source_statuses}
    assert set(statuses) == {"fast", "slow"}  # the unfinished source is in the report, not missing
    assert "did not finish" in statuses["slow"].error
    assert report.results, "hits found before the stop are kept"
    assert all(h.relevance_score > 0 for h in report.results), "and they are scored"
    assert "Results were not scored." not in report.warnings
