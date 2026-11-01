# app/utils/browser.py
"""Headless Chromium (Playwright) for the sources and pages that need a real browser.

Every page load goes through throttle(), the same per-domain limit as fetch(). Playwright is imported only when a
browser is opened: the API image does not ship it, so the import error surfaces as that one source's failure.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from app.api.models import Hit
from app.sources.base import page_problem
from app.utils.http_client import _USER_AGENTS, BlockedError, FetchError, throttle

if TYPE_CHECKING:
    from playwright.async_api import Browser, BrowserContext, Playwright

log = logging.getLogger(__name__)

_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
]
_HIDE_WEBDRIVER = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"


class ScanBrowser:
    """One headless Chromium for the work of one scan. It starts on first use, so a scan that needs no browser
    starts none, and it is closed by scan_browser() when the scan ends."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._lock = asyncio.Lock()

    async def _start(self) -> Browser:
        async with self._lock:  # sources start in parallel: only the first one launches the browser
            if self._browser is None:
                try:
                    from playwright.async_api import async_playwright
                except ImportError as exc:
                    raise FetchError(
                        "playwright not installed — run: pip install playwright && playwright install chromium"
                    ) from exc
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
            return self._browser

    async def new_context(self) -> BrowserContext:
        """A fresh context: no profile, cookies or history carry over, and downloads are refused."""
        browser = await self._start()
        context = await browser.new_context(
            user_agent=random.choice(_USER_AGENTS),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            accept_downloads=False,
        )
        await context.add_init_script(_HIDE_WEBDRIVER)
        return context

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
            if self._playwright is not None:
                await self._playwright.stop()
        except Exception as exc:  # closing must not fail the scan that is already finished
            log.warning("could not close the scan browser: %s: %s", type(exc).__name__, exc)


_scan_browser: ContextVar[ScanBrowser | None] = ContextVar("scan_browser", default=None)


@asynccontextmanager
async def scan_browser() -> AsyncIterator[ScanBrowser]:
    """Makes one ScanBrowser the browser for everything run inside this block (tasks started here inherit it),
    and closes it on exit."""
    session = ScanBrowser()
    token = _scan_browser.set(session)
    try:
        yield session
    finally:
        _scan_browser.reset(token)
        await session.close()


@asynccontextmanager
async def browser_context() -> AsyncIterator[BrowserContext]:
    """A fresh context in the scan's browser. Outside a scan it opens a browser for this one use and closes it."""
    if _scan_browser.get() is None:
        async with scan_browser():
            async with browser_context() as context:
                yield context
        return
    context = await _scan_browser.get().new_context()
    try:
        yield context
    finally:
        try:
            await context.close()
        except Exception as exc:  # a crashed browser cannot close its contexts; the scan browser's close handles it
            log.debug("context close failed: %s", exc)


async def page_html(
    context: BrowserContext,
    url: str,
    *,
    timeout_ms: int = 30000,
    wait_for_selector: str | None = None,
) -> str:
    """HTML of `url` in an open context (the caller throttles). `wait_for_selector`: results filled in by script after load are read once
    that selector is in the DOM, or as they are when it never appears (no results)."""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        if wait_for_selector:
            try:
                await page.wait_for_selector(
                    wait_for_selector, state="attached", timeout=timeout_ms
                )
            except PlaywrightTimeoutError:
                pass  # nothing rendered: the page as it is has no results
        return await page.content()
    finally:
        await page.close()


async def render_html(
    url: str, *, timeout_ms: int = 30000, wait_for_selector: str | None = None
) -> str:
    """Rendered HTML of `url` in a browser of its own (one page load)."""
    await throttle(url)
    async with browser_context() as context:
        return await page_html(
            context, url, timeout_ms=timeout_ms, wait_for_selector=wait_for_selector
        )


async def hits_from_browser(
    url: str,
    parse: Callable[[str], list[Hit]],
    *,
    wait_for_selector: str | None = None,
) -> list[Hit]:
    """Hits for `url` from its rendered page.

    A block or challenge page with no hits raises BlockedError, so the source is reported as blocked and not as
    an empty result. Any other browser failure raises FetchError.
    """
    try:
        html = await render_html(url, wait_for_selector=wait_for_selector)
    except FetchError:
        raise
    except Exception as exc:  # browser crash, page timeout, navigation error
        raise FetchError(f"browser failed: {type(exc).__name__}: {exc}") from exc
    hits = parse(html)
    if not hits and (problem := page_problem(None, "", html)):
        raise BlockedError(f"{url}: {problem}")
    return hits
