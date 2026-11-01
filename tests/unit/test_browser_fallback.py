"""Rate limits for fetch() and the rendered-page helpers that every browser source uses."""

from __future__ import annotations

import sys

import pytest

from app.utils import browser, http_client
from app.utils.http_client import BlockedError, FetchError


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: bytes = b"ok", headers: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.is_redirect = False
        self.request = None
        self.content = body

    def raise_for_status(self) -> None:
        pass

    async def aiter_bytes(self, chunk_size: int = 8192):
        yield self._body


class _FakeStream:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeClient:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls = 0

    def stream(self, method: str, url: str, **kwargs) -> _FakeStream:
        self.calls += 1
        return _FakeStream(self._responses.pop(0))


@pytest.fixture
def no_network(monkeypatch):
    """No SSRF DNS lookup, no Redis: fetch() runs against the fake client only."""
    monkeypatch.setattr(http_client, "check_url", lambda url: None)

    async def _no_limiter():
        return None

    monkeypatch.setattr(http_client, "_get_rate_limiter", _no_limiter)

    async def _no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(http_client.asyncio, "sleep", _no_sleep)


def _use_client(monkeypatch, responses: list[_FakeResponse]) -> _FakeClient:
    client = _FakeClient(responses)

    async def _get_client():
        return client

    monkeypatch.setattr(http_client, "get_client", _get_client)
    return client


# ── throttle() is the one gate for fetch() and for the browser ─────────────────

async def test_fetch_calls_throttle_before_request(monkeypatch, no_network):
    seen: list[str] = []

    async def _record(url: str) -> None:
        seen.append(url)

    monkeypatch.setattr(http_client, "throttle", _record)
    _use_client(monkeypatch, [_FakeResponse()])
    await http_client.fetch("https://apkmirror.com/search")
    assert seen == ["https://apkmirror.com/search"]


async def test_render_html_throttles_before_navigation(monkeypatch):
    seen: list[str] = []

    async def _record(url: str) -> None:
        seen.append(url)

    monkeypatch.setattr(browser, "throttle", _record)

    class _Boom(Exception):
        pass

    class _FailingPlaywright:
        async def start(self):
            raise _Boom()

    import playwright.async_api as pw

    monkeypatch.setattr(pw, "async_playwright", lambda: _FailingPlaywright())
    with pytest.raises(_Boom):
        await browser.render_html("https://www.happymod.com/search.html?q=x")
    assert seen == ["https://www.happymod.com/search.html?q=x"]


# ── 429 backs off and retries; it only becomes BlockedError when retries run out ──

async def test_429_then_success_retries(monkeypatch, no_network):
    _use_client(monkeypatch, [_FakeResponse(429, headers={"retry-after": "2"}), _FakeResponse(200, b"done")])
    resp = await http_client.fetch("https://example.com/", retries=1)
    assert resp.content == b"done"


async def test_429_exhausted_raises_blocked(monkeypatch, no_network):
    client = _use_client(monkeypatch, [_FakeResponse(429), _FakeResponse(429)])
    with pytest.raises(BlockedError, match="Rate limited"):
        await http_client.fetch("https://example.com/", retries=1)
    assert client.calls == 2


async def test_403_is_retried_then_blocked(monkeypatch, no_network):
    client = _use_client(monkeypatch, [_FakeResponse(403), _FakeResponse(403)])
    with pytest.raises(BlockedError, match="HTTP 403"):
        await http_client.fetch("https://example.com/", retries=1, backoff_base=0.0)
    assert client.calls == 2


def test_retry_after_parses_seconds_only():
    assert http_client._retry_after_seconds("7") == 7.0
    assert http_client._retry_after_seconds("-1") is None
    assert http_client._retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert http_client._retry_after_seconds(None) is None


# ── the browser reads the page; a block page is reported as blocked ───────────

def _render_as(monkeypatch, html: str) -> None:
    async def _render(url, **kw):
        return html

    monkeypatch.setattr(browser, "render_html", _render)


async def test_hits_come_from_the_rendered_page(monkeypatch):
    _render_as(monkeypatch, "<html>cards</html>")
    hits = await browser.hits_from_browser("https://x", lambda html: ["hit"] if "cards" in html else [])
    assert hits == ["hit"]


async def test_clean_empty_page_is_no_results_not_blocked(monkeypatch):
    _render_as(monkeypatch, "<html>nothing here</html>")
    assert await browser.hits_from_browser("https://x", lambda html: []) == []


async def test_block_page_without_hits_is_blocked(monkeypatch):
    _render_as(monkeypatch, "<html>Just a moment... captcha</html>")
    with pytest.raises(BlockedError):
        await browser.hits_from_browser("https://x", lambda html: [])


async def test_browser_crash_is_a_fetch_error(monkeypatch):
    async def _render(url, **kw):
        raise RuntimeError("chromium crashed")

    monkeypatch.setattr(browser, "render_html", _render)
    with pytest.raises(FetchError, match="chromium crashed"):
        await browser.hits_from_browser("https://x", lambda html: [])


async def test_missing_playwright_says_how_to_install_it(monkeypatch):
    async def _no_throttle(url):
        return None

    monkeypatch.setattr(browser, "throttle", _no_throttle)
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)  # the import then raises ImportError
    with pytest.raises(FetchError, match="playwright install chromium"):
        await browser.render_html("https://x")


async def test_render_html_refuses_downloads(monkeypatch):
    """A page that triggers a file download must not save it: the browser context is created without downloads."""
    import playwright.async_api as pw

    captured: dict = {}

    class _Page:
        async def goto(self, *a, **kw):
            return None

        async def content(self):
            return "<html></html>"

        async def close(self):
            return None

    class _Context:
        async def add_init_script(self, *a):
            return None

        async def new_page(self):
            return _Page()

    class _Browser:
        async def new_context(self, **kwargs):
            captured.update(kwargs)
            return _Context()

        async def close(self):
            return None

    class _Chromium:
        async def launch(self, **kwargs):
            return _Browser()

    class _Playwright:
        chromium = _Chromium()

        async def stop(self):
            return None

    class _Manager:
        async def start(self):
            return _Playwright()

    async def _no_throttle(url):
        return None

    monkeypatch.setattr(pw, "async_playwright", lambda: _Manager())
    monkeypatch.setattr(browser, "throttle", _no_throttle)
    await browser.render_html("https://example.com/")
    assert captured["accept_downloads"] is False


def _counting_playwright(monkeypatch) -> dict:
    """Fake Playwright that counts launches and stops, so tests can see how many browsers a scan started."""
    import playwright.async_api as pw

    counts = {"launch": 0, "stop": 0}

    class _Context:
        async def add_init_script(self, *a):
            return None

        async def close(self):
            return None

    class _Browser:
        async def new_context(self, **kwargs):
            return _Context()

        async def close(self):
            return None

    class _Chromium:
        async def launch(self, **kwargs):
            counts["launch"] += 1
            return _Browser()

    class _Playwright:
        chromium = _Chromium()

        async def stop(self):
            counts["stop"] += 1

    class _Manager:
        async def start(self):
            return _Playwright()

    monkeypatch.setattr(pw, "async_playwright", lambda: _Manager())
    return counts


async def test_scan_browser_starts_one_chromium_for_all_its_contexts(monkeypatch):
    counts = _counting_playwright(monkeypatch)
    async with browser.scan_browser():
        async with browser.browser_context():
            pass
        async with browser.browser_context():
            pass
    assert counts == {"launch": 1, "stop": 1}


async def test_scan_without_a_browser_starts_none(monkeypatch):
    counts = _counting_playwright(monkeypatch)
    async with browser.scan_browser():
        pass
    assert counts == {"launch": 0, "stop": 0}


async def test_context_outside_a_scan_gets_its_own_browser(monkeypatch):
    counts = _counting_playwright(monkeypatch)
    async with browser.browser_context():
        pass
    assert counts == {"launch": 1, "stop": 1}


async def test_sources_starting_together_launch_one_chromium(monkeypatch):
    import asyncio

    counts = _counting_playwright(monkeypatch)

    async def _use():
        async with browser.browser_context():
            await asyncio.sleep(0)

    async with browser.scan_browser():
        await asyncio.gather(_use(), _use(), _use())
    assert counts == {"launch": 1, "stop": 1}
