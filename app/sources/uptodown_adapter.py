
# app/sources/uptodown_adapter.py
"""Uptodown adapter — Playwright Android app store scraper.

Uptodown does not return useful data via static HTTP (JS-rendered).
This adapter uses async Playwright; the host needs `playwright install chromium`.
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
from app.sources.base import SourceAdapter

from app.utils.http_client import throttle

log = logging.getLogger(__name__)

try:
    from playwright.async_api import async_playwright as _async_playwright
    _PLAYWRIGHT_OK = True
except ImportError:
    _async_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_OK = False

_PKG_RE = re.compile(
    r'\b((?:com|net|org|in|io|co)\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]+)+)\b'
)
_DOMAIN_TLDS = {".com", ".net", ".org", ".io", ".co", ".in"}
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

def _pkg_from_text(text: str) -> str:
    for m in _PKG_RE.finditer(text):
        pkg = m.group(1)
        if not any(pkg.endswith(t) for t in _DOMAIN_TLDS):
            return pkg
    return ""


class UptodownAdapter(SourceAdapter):
    name = "uptodown"
    method = SourceMethod.scrape
    host_group = "apk_site"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        if not _PLAYWRIGHT_OK:
            log.warning("uptodown: playwright not installed — adapter unavailable")
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed",
                error="playwright not installed — run: pip install playwright && playwright install chromium",
            )

        async_playwright = _async_playwright

        brand_tokens = {t.lower() for t in ctx.request.brand_name.split() if len(t) >= 3}
        terms = [
            kw.term for kw in ctx.expanded_keywords
            if kw.type.value in ("original", "lure")
            and any(tok in kw.term.lower() for tok in brand_tokens)
        ][:5]

        hits: list[Hit] = []
        seen_pkgs: set[str] = set()

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
                )
                bctx = await browser.new_context(
                    accept_downloads=False,
                    user_agent=_UA, viewport={"width": 1280, "height": 800}, locale="en-US"
                )

                for term in terms:
                    search_url = f"https://en.uptodown.com/android/search?query={quote_plus(term)}"

                    app_urls: list[str] = []
                    page = await bctx.new_page()
                    try:
                        await throttle(search_url)
                        await page.goto(search_url, wait_until="networkidle", timeout=30000)
                        for a in await page.query_selector_all("a[href*='.en.uptodown.com/android']"):
                            href = await a.get_attribute("href") or ""
                            if href and href not in app_urls:
                                app_urls.append(href)
                    except Exception as e:
                        log.warning("uptodown: search page parse failed for term %r: %s", term, e)
                    finally:
                        await page.close()

                    for app_url in app_urls[:8]:
                        app_page = await bctx.new_page()
                        try:
                            await throttle(app_url)
                            await app_page.goto(app_url, wait_until="domcontentloaded", timeout=20000)

                            # Try explicit package table cell first, then regex fallback
                            pkg = ""
                            pkg_el = await app_page.query_selector(
                                "th:text('Package Name') + td, th:text('Package name') + td"
                            )
                            if pkg_el:
                                pkg = (await pkg_el.text_content() or "").strip()
                            if not pkg:
                                pkg = _pkg_from_text(await app_page.content())
                            if not pkg or pkg in seen_pkgs:
                                continue
                            seen_pkgs.add(pkg)

                            name = ""
                            name_el = await app_page.query_selector("#detail-app-name, h1")
                            if name_el:
                                name = (await name_el.text_content() or "").strip()

                            dev = ""
                            dev_el = await app_page.query_selector("#author-link, .autor button, .autor a")
                            if dev_el:
                                dev = (await dev_el.text_content() or "").strip()

                            icon_url: str | None = None
                            icon_el = await app_page.query_selector("img[itemprop='image']")
                            if icon_el:
                                icon_url = await icon_el.get_attribute("src") or None

                            uid = hashlib.sha256(app_url.encode()).hexdigest()[:32]
                            hits.append(Hit(
                                id=uid,
                                type=HitType.app_listing,
                                title=name or pkg,
                                app_name=name or None,
                                package_id=pkg,
                                publisher=dev or None,
                                icon_url=icon_url,
                                url=app_url,
                                host=HostInfo(
                                    group=self.host_group,
                                    name="Uptodown",
                                    domain="uptodown.com",
                                    is_official_store=False,
                                ),
                                found_via=[FoundVia(source=self.name, method=self.method, query=term)],
                                relevance_score=0,
                                discovered_at=datetime.datetime.now(datetime.timezone.utc),
                            ))
                        except Exception as e:
                            log.warning("uptodown: app page parse failed for %r: %s", app_url, e)
                        finally:
                            await app_page.close()

                await browser.close()

        except Exception as exc:
            log.warning("uptodown: scrape failed: %s", exc)
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed",
                error=f"Playwright error: {exc}",
            )

        return hits, SourceStatus(
            name=self.name, method=self.method, status="ok", results=len(hits)
        )
