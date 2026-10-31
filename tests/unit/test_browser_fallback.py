"""Browser fallback and rate-limit coverage: httpx first, Playwright only when httpx gives no results."""

from __future__ import annotations

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
        async def __aenter__(self):
            raise _Boom()

        async def __aexit__(self, *exc):
            return None

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


async def test_403_is_not_retried(monkeypatch, no_network):
    client = _use_client(monkeypatch, [_FakeResponse(403)])
    with pytest.raises(BlockedError, match="HTTP 403"):
        await http_client.fetch("https://example.com/", retries=2)
    assert client.calls == 1


def test_retry_after_parses_seconds_only():
    assert http_client._retry_after_seconds("7") == 7.0
    assert http_client._retry_after_seconds("-1") is None
    assert http_client._retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert http_client._retry_after_seconds(None) is None


# ── httpx first; the browser only when httpx gives no results ──────────────────

async def test_hits_from_httpx_skip_browser(monkeypatch):
    async def _fetch(url, **kw):
        return _FakeResponse(body=b"<html>results</html>")

    async def _render(url, **kw):
        raise AssertionError("browser must not run when httpx has results")

    monkeypatch.setattr(browser, "fetch", _fetch)
    monkeypatch.setattr(browser, "render_html", _render)
    hits = await browser.hits_with_browser_fallback("https://x", lambda html: ["hit"])
    assert hits == ["hit"]


async def test_empty_httpx_falls_back_to_browser(monkeypatch):
    async def _fetch(url, **kw):
        return _FakeResponse(body=b"<html>empty</html>")

    async def _render(url, **kw):
        return "<html>rendered</html>"

    monkeypatch.setattr(browser, "fetch", _fetch)
    monkeypatch.setattr(browser, "render_html", _render)
    hits = await browser.hits_with_browser_fallback(
        "https://x", lambda html: ["rendered"] if "rendered" in html else [],
    )
    assert hits == ["rendered"]


async def test_blocked_httpx_and_empty_browser_stays_blocked(monkeypatch):
    async def _fetch(url, **kw):
        raise BlockedError("HTTP 403 from https://x")

    async def _render(url, **kw):
        return "<html>no cards</html>"

    monkeypatch.setattr(browser, "fetch", _fetch)
    monkeypatch.setattr(browser, "render_html", _render)
    with pytest.raises(BlockedError, match="HTTP 403"):
        await browser.hits_with_browser_fallback("https://x", lambda html: [])


async def test_blocked_httpx_and_browser_failure_stays_blocked(monkeypatch):
    async def _fetch(url, **kw):
        raise BlockedError("HTTP 403 from https://x")

    async def _render(url, **kw):
        raise RuntimeError("playwright not installed")

    monkeypatch.setattr(browser, "fetch", _fetch)
    monkeypatch.setattr(browser, "render_html", _render)
    with pytest.raises(BlockedError):
        await browser.hits_with_browser_fallback("https://x", lambda html: [])


async def test_clean_empty_httpx_page_tries_browser_and_returns_nothing(monkeypatch):
    rendered_urls: list[str] = []

    async def _fetch(url, **kw):
        return _FakeResponse(body=b"<html>no matches</html>")

    async def _render(url, **kw):
        rendered_urls.append(url)
        return "<html>still no matches</html>"

    monkeypatch.setattr(browser, "fetch", _fetch)
    monkeypatch.setattr(browser, "render_html", _render)
    assert await browser.hits_with_browser_fallback("https://x", lambda html: []) == []
    assert rendered_urls == ["https://x"]


async def test_html_fallback_returns_rendered_page_when_it_has_results(monkeypatch):
    async def _fetch(url, **kw):
        return _FakeResponse(body=b"<html>empty</html>")

    async def _render(url, **kw):
        return "<html>videos</html>"

    monkeypatch.setattr(browser, "fetch", _fetch)
    monkeypatch.setattr(browser, "render_html", _render)
    html = await browser.html_with_browser_fallback("https://x", lambda page: "videos" in page)
    assert html == "<html>videos</html>"


async def test_html_fallback_raises_when_httpx_blocked_and_browser_empty(monkeypatch):
    async def _fetch(url, **kw):
        raise FetchError("network down")

    async def _render(url, **kw):
        return "<html>nothing</html>"

    monkeypatch.setattr(browser, "fetch", _fetch)
    monkeypatch.setattr(browser, "render_html", _render)
    with pytest.raises(FetchError, match="network down"):
        await browser.html_with_browser_fallback("https://x", lambda page: False)


async def test_render_html_refuses_downloads(monkeypatch):
    """A page that triggers a file download must not save it: the browser context is created without downloads."""
    import playwright.async_api as pw

    captured: dict = {}

    class _Page:
        async def goto(self, *a, **kw):
            return None

        async def content(self):
            return "<html></html>"

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

    class _Manager:
        async def __aenter__(self):
            return _Playwright()

        async def __aexit__(self, *exc):
            return None

    async def _no_throttle(url):
        return None

    monkeypatch.setattr(pw, "async_playwright", lambda: _Manager())
    monkeypatch.setattr(browser, "throttle", _no_throttle)
    await browser.render_html("https://example.com/")
    assert captured["accept_downloads"] is False
