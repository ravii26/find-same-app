"""Softonic adapter — Playwright scraper.

Multi-platform (Android, iOS, Windows, Mac). Widely indexed by Google;
appears in search results for "APK download".

Plain HTTP is refused (412), so the search page is loaded in headless Chromium. Softonic also answers
406/412 to a client it has decided to throttle; that is reported as `blocked`, never as an empty result.

Search URL: https://en.softonic.com/s/{query}:android
Result:     <a href="https://{slug}.en.softonic.com/android"> wrapping <img alt="Icon of program: {name}">
Auth: None
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import re
from urllib.parse import quote

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
    pkg_queries,
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

_BASE = "https://en.softonic.com"
_SEARCH = _BASE + "/s/{q}:android"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# App pages live on a per-app subdomain: https://whatsapp.en.softonic.com/android
_APP_URL_RE = re.compile(r"^https://([a-z0-9-]+)\.en\.softonic\.com/android/?$")
_APP_LINK_SEL = "a[href*='.en.softonic.com/android']"
_ICON_ALT_PREFIX = "Icon of program:"

# One round trip: every result anchor with its icon alt text (the cleanest source of the app name).
_EXTRACT_JS = """els => els.map(a => ({
    href: a.href,
    alt: (a.querySelector('img') || {}).alt || '',
    text: (a.innerText || '').trim().replace(/\\s+/g, ' '),
    icon: (a.querySelector('img') || {}).src || '',
}))"""

_MAX_CARDS = 30


def parse_results(rows: list[dict]) -> list[dict]:
    """Anchor rows from the page -> unique app results (name, url, icon). Pure, so it is unit-testable."""
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        href = (row.get("href") or "").rstrip("/")
        m = _APP_URL_RE.match(href)
        if not m or href in seen:
            continue
        seen.add(href)
        alt = (row.get("alt") or "").strip()
        name = alt.removeprefix(_ICON_ALT_PREFIX).strip() if alt else ""
        if not name:
            name = m.group(1).replace("-", " ").title()
        out.append({"name": name, "url": href, "slug": m.group(1), "icon_url": row.get("icon") or None})
    return out


class SoftonicAdapter(SourceAdapter):
    name = "softonic"
    method = SourceMethod.scrape
    host_group = "apk_site"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        if not _PLAYWRIGHT_OK:
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed",
                error="playwright not installed — run: pip install playwright && playwright install chromium",
            )

        brand = ctx.request.brand_name
        brand_tokens = {t.lower() for t in fold_homoglyphs(brand).split() if len(t) >= 3}

        hits: list[Hit] = []
        seen: set[str] = set()
        errors: list[str] = []
        blocked: list[str] = []

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

                for q in pkg_queries(ctx)[:2]:
                    page = await bctx.new_page()
                    try:
                        try:
                            await throttle(_SEARCH.format(q=quote(q)))
                            resp = await page.goto(
                                _SEARCH.format(q=quote(q)), wait_until="domcontentloaded", timeout=30000,
                            )
                        except Exception as e:
                            (blocked if is_network_block(str(e)) else errors).append(f"{type(e).__name__}: {str(e)[:120]}")
                            if is_network_block(str(e)):
                                break  # the site is unreachable from here; the second query will not differ
                            continue

                        status = resp.status if resp else None
                        problem = page_problem(status, await page.title())
                        if problem:
                            blocked.append(f"{problem} from {_BASE}")
                            continue

                        try:
                            await page.wait_for_selector(_APP_LINK_SEL, state="attached", timeout=12000)
                        except Exception:
                            body = await page.inner_text("body")
                            problem = page_problem(status, await page.title(), body)
                            if problem:
                                blocked.append(f"{problem} from {_BASE}")
                            elif not says_no_results(body):
                                errors.append(f"no result links recognised for {q!r} (page layout changed?)")
                            continue

                        rows = await page.eval_on_selector_all(_APP_LINK_SEL, _EXTRACT_JS)
                        for r in parse_results(rows)[:_MAX_CARDS]:
                            name_lower = fold_homoglyphs(r["name"]).lower()
                            slug_lower = r["slug"].lower()
                            if brand_tokens and not any(
                                tok in name_lower or tok in slug_lower for tok in brand_tokens
                            ):
                                continue
                            uid = hashlib.sha256(r["url"].encode()).hexdigest()[:32]
                            if uid in seen:
                                continue
                            seen.add(uid)
                            hits.append(Hit(
                                id=uid,
                                type=HitType.app_listing,
                                app_name=r["name"],
                                package_id=None,
                                icon_url=r["icon_url"],
                                url=r["url"],
                                host=HostInfo(
                                    group=self.host_group,
                                    name="Softonic",
                                    domain="softonic.com",
                                    is_official_store=False,
                                ),
                                found_via=[FoundVia(source=self.name, method=self.method, query=q)],
                                relevance_score=0,
                                discovered_at=datetime.datetime.now(datetime.timezone.utc),
                            ))

                    except Exception as e:
                        log.warning("softonic: search failed for query %r: %s", q, e)
                        errors.append(f"{type(e).__name__}: {str(e)[:120]}")
                    finally:
                        await page.close()

                await browser.close()

        except Exception as exc:
            log.warning("softonic: scrape failed: %s", exc)
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed",
                error=f"Playwright error: {exc}",
            )

        return hits, scrape_status(self.name, self.method, hits, errors=errors, blocked=blocked)
