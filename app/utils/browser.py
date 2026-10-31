# app/utils/browser.py
"""Rendered-page (Playwright) helpers for sources that need a real browser.

Browser navigation is rate-limited by the same per-domain limit as fetch(): throttle() runs before
every page load. Playwright is a fallback, not a first step: hits_with_browser_fallback() asks for the
page over httpx first and renders it only when that returns no results.
"""

from __future__ import annotations

import logging
import random
import re
from collections.abc import Callable

from app.api.models import Hit
from app.utils.http_client import _USER_AGENTS, BlockedError, FetchError, decode_response, fetch, throttle

log = logging.getLogger(__name__)

_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-blink-features=AutomationControlled",
]

_SHOW_MORE_RE = re.compile(r"^(see|show|load) more$", re.IGNORECASE)


async def render_html(
    url: str, *, wait_until: str = "domcontentloaded", timeout_ms: int = 30000, wait_for_selector: str | None = None,
) -> str:
    """Load `url` in headless Chromium and return the rendered HTML.

    `wait_for_selector` is for pages whose results are filled in by script after load: the page is read
    once that selector is in the DOM, or as it is when it never appears (no results).

    Playwright is imported here, not at module level: the api image ships without it, and an import
    error then surfaces as a failed fallback instead of breaking the whole app at startup.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    await throttle(url)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        try:
            # accept_downloads=False: a page that triggers a file download must not save that file to disk.
            context = await browser.new_context(
                user_agent=random.choice(_USER_AGENTS), viewport={"width": 1280, "height": 800}, locale="en-US",
                accept_downloads=False,
            )
            await context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = await context.new_page()
            await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            if wait_for_selector:
                try:
                    await page.wait_for_selector(wait_for_selector, state="attached", timeout=timeout_ms)
                except PlaywrightTimeoutError:
                    pass  # nothing rendered: the page as it is has no results
            return await page.content()
        finally:
            await browser.close()


async def collect_while_scrolling(
    url: str, pattern: re.Pattern[str], *, max_rounds: int = 40, pause_ms: int = 1500, timeout_ms: int = 30000,
) -> tuple[list[str], bool]:
    """Scroll `url` in headless Chromium and collect the first group of `pattern` from every round's HTML.

    Lists that fill in as the reader scrolls (or after a "Show more" click) are read until a few rounds in
    a row add nothing, or until `max_rounds` is reached. Returns (matches in page order, complete): complete
    is False when the round cap was hit while the page was still adding matches.
    """
    from playwright.async_api import async_playwright

    await throttle(url)
    seen: dict[str, None] = {}
    quiet_rounds = 0
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
        try:
            context = await browser.new_context(
                user_agent=random.choice(_USER_AGENTS), viewport={"width": 1280, "height": 800}, locale="en-US",
                accept_downloads=False,
            )
            await context.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(pause_ms)
            for _ in range(max_rounds):
                before = len(seen)
                for match in pattern.findall(await page.content()):
                    seen.setdefault(match, None)
                quiet_rounds = 0 if len(seen) > before else quiet_rounds + 1
                if quiet_rounds >= 3:
                    return list(seen), True
                more = page.get_by_role("button", name=_SHOW_MORE_RE)
                if await more.count():
                    try:
                        await more.first.click(timeout=2000)
                    except Exception as exc:  # the button went away or is covered: the next scroll still reads on
                        log.debug("show-more click on %s failed: %s", url, exc)
                await page.mouse.wheel(0, 4000)
                await page.wait_for_timeout(pause_ms)
            return list(seen), False
        finally:
            await browser.close()


async def html_with_browser_fallback(
    url: str, has_results: Callable[[str], bool], **fetch_kwargs,
) -> str:
    """Page HTML for `url`: httpx first, Playwright only when httpx gives no results.

    `has_results` decides what "results" means for the page. Raises BlockedError/FetchError only when
    httpx failed and the browser found nothing either, so the adapter still reports a block as blocked.
    An empty httpx page counts as "no results" too, so the browser is tried for it.
    """
    error: FetchError | None = None
    html = ""
    try:
        html = decode_response(await fetch(url, **fetch_kwargs))
        if has_results(html):
            return html
    except (BlockedError, FetchError) as exc:
        error = exc

    log.info("no results over httpx for %s, trying the browser", url)
    try:
        rendered = await render_html(url)
    except Exception as exc:  # Playwright missing, browser crash, page timeout: the httpx outcome stands
        log.warning("browser fallback failed for %s: %s", url, exc)
        if error is not None:
            raise error
        return html
    if has_results(rendered):
        return rendered
    if error is not None:
        raise error
    return html


async def hits_with_browser_fallback(
    url: str, parse: Callable[[str], list[Hit]], *, browser_wait_for: str | None = None, **fetch_kwargs,
) -> list[Hit]:
    """Hits for `url`: httpx first, Playwright only when httpx gives no results.

    Same rules as html_with_browser_fallback, with "results" meaning `parse` returned hits.
    `browser_wait_for` is the selector the browser waits for (see render_html) when results are script-rendered.
    """
    error: FetchError | None = None
    try:
        hits = parse(decode_response(await fetch(url, **fetch_kwargs)))
    except (BlockedError, FetchError) as exc:
        hits, error = [], exc
    if hits:
        return hits

    log.info("no results over httpx for %s, trying the browser", url)
    try:
        hits = parse(await render_html(url, wait_for_selector=browser_wait_for))
    except Exception as exc:  # Playwright missing, browser crash, page timeout: the httpx outcome stands
        log.warning("browser fallback failed for %s: %s", url, exc)
        if error is not None:
            raise error
        return []
    if not hits and error is not None:
        raise error
    return hits
