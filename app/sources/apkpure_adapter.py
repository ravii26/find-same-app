"""APKPure adapter — Playwright scraper.

APKPure is protected by Cloudflare TLS fingerprinting so static HTTP requests
are rejected. Playwright (real Chromium) bypasses this.

APKPure URL format: https://apkpure.com/{app-slug}/{package.id}
The package ID is embedded in every search result link — no per-app page
visit needed, which keeps this adapter fast.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import re

from app.api.models import (
    FoundVia,
    Hit,
    HitType,
    HostInfo,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.services.keyword_expansion import fold_homoglyphs
from app.sources.base import (
    SourceAdapter,
    is_network_block,
    page_problem,
    says_no_results,
    scrape_status,
)

from app.utils.http_client import throttle

log = logging.getLogger(__name__)

try:
    from playwright.async_api import async_playwright as _async_playwright
    _PLAYWRIGHT_OK = True
except ImportError:
    _async_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_OK = False

_BASE = "https://apkpure.com"
_SEARCH = "https://apkpure.com/search?q={q}"

# Extract package ID from APKPure URL: /app-slug/com.package.id
_PKG_RE = re.compile(
    r'/([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,})/?$',
    re.IGNORECASE,
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _pkg_from_url(url: str) -> str | None:
    """Extract package ID from APKPure URL path."""
    m = _PKG_RE.search(url)
    if m:
        pkg = m.group(1)
        # Must have at least 2 dot-separated components and start with known prefix
        parts = pkg.split(".")
        if len(parts) >= 2 and parts[0].lower() in (
            "com", "net", "org", "in", "io", "co", "app"
        ):
            return pkg
    return None


class APKPureAdapter(SourceAdapter):
    name = "apkpure"
    method = SourceMethod.scrape
    host_group = "apk_site"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        if not _PLAYWRIGHT_OK:
            log.warning("apkpure: playwright not installed — adapter unavailable")
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed",
                error="playwright not installed — run: pip install playwright && playwright install chromium",
            )

        async_playwright = _async_playwright

        brand = ctx.request.brand_name
        brand_tokens = {t.lower() for t in fold_homoglyphs(brand).split() if len(t) >= 3}

        # Queries: brand name + last package component if different
        queries: list[str] = [brand]
        pkg = ctx.request.android_package or ""
        if pkg:
            last = pkg.split(".")[-1]
            if last.lower() != brand.lower():
                queries.append(last)

        hits: list[Hit] = []
        seen: set[str] = set()
        errors: list[str] = []
        blocked: list[str] = []

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
                bctx = await browser.new_context(
                    accept_downloads=False,
                    user_agent=_UA,
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )
                await bctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )

                for q in queries[:2]:
                    page = await bctx.new_page()
                    try:
                        await throttle(_SEARCH.format(q=q))
                        resp = await page.goto(
                            _SEARCH.format(q=q),
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                    except Exception as e:
                        # A timeout / TLS failure here is the site (or its CDN) refusing this network, not "no results"
                        if is_network_block(str(e)):
                            blocked.append(f"unreachable: {str(e).splitlines()[0][:110]}")
                            await page.close()
                            break  # every query hits the same wall
                        errors.append(f"{type(e).__name__}: {str(e).splitlines()[0][:110]}")
                        await page.close()
                        continue

                    status = resp.status if resp else None
                    problem = page_problem(status, await page.title())
                    if problem:
                        blocked.append(f"{problem} from {_BASE}")
                        await page.close()
                        continue

                    try:
                        await page.wait_for_selector(
                            ".search-result-item, .applist li, ul#search-res li",
                            timeout=10000,
                        )
                    except Exception:
                        body = await page.inner_text("body")
                        problem = page_problem(status, await page.title(), body)
                        if problem:
                            blocked.append(f"{problem} from {_BASE}")
                        elif not says_no_results(body):
                            errors.append(f"no result cards recognised for {q!r} (page layout changed?)")
                        await page.close()
                        continue

                    try:
                        # Collect all result card links
                        anchors = await page.query_selector_all(
                            ".search-result-item a[href], .applist li a[href], ul#search-res li a[href]"
                        )

                        for anchor in anchors[:20]:
                            href = await anchor.get_attribute("href") or ""
                            if not href:
                                continue
                            full_url = href if href.startswith("http") else f"{_BASE}{href}"

                            # Skip non-app URLs (categories, developer pages, etc.)
                            pkg_id = _pkg_from_url(full_url)
                            if not pkg_id:
                                continue

                            uid = hashlib.sha256(full_url.encode()).hexdigest()[:32]
                            if uid in seen:
                                continue
                            seen.add(uid)

                            # App name — nearest text node or sibling
                            name: str | None = None
                            for sel in [
                                ".p1", ".app-name", "h4", ".title",
                                ".search-title", "strong",
                            ]:
                                try:
                                    el = await anchor.query_selector(sel)
                                    if el:
                                        name = (await el.text_content() or "").strip() or None
                                        if name:
                                            break
                                except Exception as e:
                                    log.debug("apkpure: failed to read name selector %r: %s", sel, e)
                                    continue

                            # Brand-token filter
                            name_lower = fold_homoglyphs(name or "").lower()
                            pkg_lower = pkg_id.lower()
                            if brand_tokens and not any(
                                tok in name_lower or tok in pkg_lower
                                for tok in brand_tokens
                            ):
                                continue

                            # Developer
                            publisher: str | None = None
                            for sel in [".p2", ".developer", ".author"]:
                                try:
                                    el = await anchor.query_selector(sel)
                                    if el:
                                        publisher = (await el.text_content() or "").strip() or None
                                        if publisher:
                                            break
                                except Exception as e:
                                    log.debug("apkpure: failed to read publisher selector %r: %s", sel, e)
                                    continue

                            # Icon
                            icon_url: str | None = None
                            try:
                                img = await anchor.query_selector("img")
                                if img:
                                    icon_url = await img.get_attribute("src") or None
                            except Exception as e:
                                log.debug("apkpure: failed to read icon for %r: %s", full_url, e)

                            hits.append(Hit(
                                id=uid,
                                type=HitType.app_listing,
                                app_name=name,
                                package_id=pkg_id,
                                publisher=publisher,
                                icon_url=icon_url,
                                url=full_url,
                                host=HostInfo(
                                    group=self.host_group,
                                    name="APKPure",
                                    domain="apkpure.com",
                                    is_official_store=False,
                                ),
                                found_via=[
                                    FoundVia(source=self.name, method=self.method, query=q)
                                ],
                                relevance_score=0,
                                discovered_at=datetime.datetime.now(datetime.timezone.utc),
                            ))

                    except Exception as e:
                        log.warning("apkpure: failed to parse results for query %r: %s", q, e)
                        errors.append(f"result parsing failed: {type(e).__name__}")
                    finally:
                        await page.close()

                await browser.close()

        except Exception as exc:
            log.warning("apkpure: scrape failed: %s", exc)
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed",
                error=f"Playwright error: {exc}",
            )

        return hits, scrape_status(self.name, self.method, hits, errors=errors, blocked=blocked)
