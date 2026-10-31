"""HappyMod adapter — Playwright scraper.

HappyMod specialises in modified/modded APKs and is a primary redistribution
channel for rogue app clones. Static HTTP returns bot-challenge pages.

Search URL: https://www.happymod.com/search.html?q={query}
App pages:  https://www.happymod.com/{slug}-mod.html
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
from app.services.keyword_expansion import fold_homoglyphs
from app.sources.base import SourceAdapter, pkg_queries

from app.utils.http_client import throttle

log = logging.getLogger(__name__)

try:
    from playwright.async_api import async_playwright as _async_playwright
    _PLAYWRIGHT_OK = True
except ImportError:
    _async_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_OK = False

_BASE = "https://www.happymod.com"
_SEARCH = _BASE + "/search.html?q={q}"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_PKG_RE = re.compile(
    r'\b((?:com|net|org|in|io|app)\.[a-zA-Z][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]+)+)\b'
)

_CARD_SELECTORS = [".mod-item", ".app-item", "[class*='mod-item']", "[class*='app-item']"]
_NAME_SELECTORS = ["h3", "h4", "[class*='name']", "[class*='title']", "p.title"]


def _extract_pkg(text: str) -> str | None:
    m = _PKG_RE.search(text)
    return m.group(1) if m else None


class HappyModAdapter(SourceAdapter):
    name = "happymod"
    method = SourceMethod.scrape
    host_group = "apk_site"
    max_results = 10

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        if not _PLAYWRIGHT_OK:
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed",
                error="playwright not installed — run: pip install playwright && playwright install chromium",
            )

        brand = ctx.request.brand_name
        brand_tokens = {t.lower() for t in fold_homoglyphs(brand).split() if len(t) >= 3}

        queries = pkg_queries(ctx)

        hits: list[Hit] = []
        seen: set[str] = set()
        errors: list[str] = []

        try:
            async with _async_playwright() as p:
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

                detail_visits = 0

                for q in queries[:2]:
                    page = await bctx.new_page()
                    try:
                        await throttle(_SEARCH.format(q=quote_plus(q)))
                        await page.goto(
                            _SEARCH.format(q=quote_plus(q)),
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )

                        # Try each card selector
                        card_sel: str | None = None
                        for sel in _CARD_SELECTORS:
                            try:
                                await page.wait_for_selector(sel, timeout=10000)
                                card_sel = sel
                                break
                            except Exception:
                                continue

                        if not card_sel:
                            log.debug("happymod: no app cards found for query %r", q)
                            await page.close()
                            continue

                        cards = await page.query_selector_all(card_sel)
                        for card in cards[:20]:
                            # Extract name
                            name: str | None = None
                            for nsel in _NAME_SELECTORS:
                                el = await card.query_selector(nsel)
                                if el:
                                    name = (await el.text_content() or "").strip() or None
                                    if name:
                                        break
                            if not name:
                                title_attr = await card.get_attribute("title")
                                name = (title_attr or "").strip() or None
                            if not name:
                                continue

                            name_lower = fold_homoglyphs(name).lower()
                            if brand_tokens and not any(tok in name_lower for tok in brand_tokens):
                                continue

                            # Extract href
                            href: str | None = None
                            a_el = await card.query_selector("a[href]")
                            if a_el:
                                href = await a_el.get_attribute("href") or None
                            if not href:
                                href = await card.get_attribute("href") or None
                            if not href:
                                continue

                            app_url = href if href.startswith("http") else f"{_BASE}{href}"
                            uid = hashlib.sha256(app_url.encode()).hexdigest()[:32]
                            if uid in seen:
                                continue
                            seen.add(uid)

                            # Try to get package ID from href first
                            pkg_id = _extract_pkg(href)

                            # Visit detail page for package ID if not found (cap at 5)
                            if not pkg_id and detail_visits < 5:
                                detail_visits += 1
                                detail = await bctx.new_page()
                                try:
                                    await throttle(app_url)
                                    await detail.goto(app_url, wait_until="domcontentloaded", timeout=20000)
                                    content = await detail.content()
                                    pkg_id = _extract_pkg(content)
                                except Exception as e:
                                    log.debug("happymod: detail page failed for %s: %s", app_url, e)
                                finally:
                                    await detail.close()

                            hits.append(Hit(
                                id=uid,
                                type=HitType.app_listing,
                                app_name=name,
                                package_id=pkg_id,
                                url=app_url,
                                host=HostInfo(
                                    group=self.host_group,
                                    name="HappyMod",
                                    domain="happymod.com",
                                    is_official_store=False,
                                ),
                                found_via=[FoundVia(source=self.name, method=self.method, query=q)],
                                relevance_score=0,
                                discovered_at=datetime.datetime.now(datetime.timezone.utc),
                            ))

                    except Exception as e:
                        log.warning("happymod: search failed for query %r: %s", q, e)
                        errors.append(str(e))
                    finally:
                        await page.close()

                await browser.close()

        except Exception as exc:
            log.warning("happymod: scrape failed: %s", exc)
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed",
                error=f"Playwright error: {exc}",
            )

        if errors and not hits:
            status_val, err_msg = "failed", errors[0]
        elif errors:
            status_val, err_msg = "partial", errors[0]
        else:
            status_val, err_msg = "ok", None

        return hits, SourceStatus(
            name=self.name, method=self.method, status=status_val,
            results=len(hits), error=err_msg,
        )
