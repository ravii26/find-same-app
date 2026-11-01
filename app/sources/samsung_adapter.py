"""Samsung Galaxy Store adapter — Playwright scraper.

Samsung's store is a fully JS-rendered React SPA with no public JSON API.
This adapter uses async Playwright to navigate the search page and extract
app listings. The host needs `playwright install chromium`.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import re
from urllib.parse import quote_plus

from app.api.models import (
    FoundVia,
    Hit,
    HitType,
    HostInfo,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.sources.base import SourceAdapter, note_page_error, page_problem, scrape_status

from app.utils.http_client import throttle

log = logging.getLogger(__name__)

try:
    from playwright.async_api import async_playwright as _async_playwright
    _PLAYWRIGHT_OK = True
except ImportError:
    _async_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_OK = False

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# Package names embedded in Galaxy Store URLs: /detail/G000XXXXXX or data attrs
_PKG_RE = re.compile(r'\b((?:com|net|org|in|io|co)\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]+)+)\b')
_DOMAIN_TLDS = {".com", ".net", ".org", ".io", ".co", ".in"}


def _pkg_from_text(text: str) -> str:
    for m in _PKG_RE.finditer(text):
        pkg = m.group(1)
        if not any(pkg.endswith(t) for t in _DOMAIN_TLDS):
            return pkg
    return ""


class SamsungAdapter(SourceAdapter):
    name = "samsung_galaxy"
    method = SourceMethod.scrape
    host_group = "app_store"
    max_results = 10  # official store — expect 1 official app + a few lookalikes

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        if not _PLAYWRIGHT_OK:
            log.warning("samsung: playwright not installed — adapter unavailable")
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed",
                error="playwright not installed — run: pip install playwright && playwright install chromium",
            )

        async_playwright = _async_playwright

        brand_tokens = {t.lower() for t in ctx.request.brand_name.split() if len(t) >= 3}
        terms = [
            kw.term for kw in ctx.expanded_keywords
            if kw.type.value in ("original", "package")
            and any(tok in kw.term.lower() for tok in brand_tokens)
        ][:3]

        if not terms:
            return [], SourceStatus(name=self.name, method=self.method, status="skipped",
                                    error="no brand-relevant keywords")

        hits: list[Hit] = []
        seen: set[str] = set()
        errors: list[str] = []
        blocked: list[str] = []

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage",
                          "--disable-gpu", "--no-zygote",
                          "--disable-blink-features=AutomationControlled"],
                )
                bctx = await browser.new_context(
                    accept_downloads=False,
                    user_agent=_UA,
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                )
                await bctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )

                for term in terms:
                    search_url = (
                        f"https://galaxystore.samsung.com/search?q={quote_plus(term)}"
                    )
                    page = await bctx.new_page()
                    try:
                        await throttle(search_url)
                        resp = await page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
                        problem = page_problem(resp.status if resp else None, await page.title())
                        if problem:
                            blocked.append(f"{problem} from galaxystore.samsung.com")
                            continue
                        # Give JS time to render results; networkidle can hang on SPAs
                        try:
                            await page.wait_for_load_state("networkidle", timeout=20000)
                        except Exception:
                            pass  # proceed with whatever rendered

                        content = await page.content()
                        log.debug("samsung: page loaded for %r, content length=%d", term, len(content))

                        # Extract all Samsung app detail links
                        links = await page.query_selector_all("a[href*='/detail/']")
                        for link in links[:20]:
                            href = await link.get_attribute("href") or ""
                            if "/detail/" not in href:
                                continue

                            full_url = (
                                href if href.startswith("http")
                                else f"https://galaxystore.samsung.com{href}"
                            )
                            uid = hashlib.sha256(full_url.encode()).hexdigest()[:32]
                            if uid in seen:
                                continue
                            seen.add(uid)

                            # Try to find the nearest text for app name
                            name = ""
                            try:
                                name_el = await link.query_selector(
                                    "[class*='name'], [class*='title'], span, p"
                                )
                                if name_el:
                                    name = (await name_el.text_content() or "").strip()
                            except Exception as e:
                                log.debug("samsung: failed to read app name for %r: %s", full_url, e)

                            # Package name sometimes embedded in data attrs or page source
                            pkg = _pkg_from_text(href) or _pkg_from_text(content[:8000])

                            hits.append(Hit(
                                id=uid,
                                type=HitType.app_listing,
                                title=name or None,
                                app_name=name or None,
                                package_id=pkg or None,
                                url=full_url,
                                host=HostInfo(
                                    group=self.host_group,
                                    name="Samsung Galaxy Store",
                                    domain="galaxystore.samsung.com",
                                    is_official_store=False,
                                ),
                                found_via=[FoundVia(
                                    source=self.name, method=self.method, query=term
                                )],
                                relevance_score=0,
                                discovered_at=datetime.datetime.now(datetime.timezone.utc),
                            ))

                    except Exception as e:
                        log.warning("samsung: failed to load/parse results for term %r: %s", term, e)
                        note_page_error(e, errors, blocked)
                    finally:
                        await page.close()

                await browser.close()

        except Exception as exc:
            log.warning("samsung: scrape failed: %s", exc)
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed",
                error=f"Playwright error: {exc}",
            )

        # A page that never loaded is not "no results": report it as blocked/failed (or partial with some hits)
        return hits, scrape_status(self.name, self.method, hits, errors=errors, blocked=blocked)
