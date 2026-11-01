"""PhonePe Appstore (formerly Indus Appstore) adapter — Playwright scraper.

Indus Appstore is live on 100M+ devices in India (pre-installed on Xiaomi, Redmi, Poco, Lava and Motorola India).
It is a P1 Critical source for India-focused rogue app monitoring.

The search page is rendered in Chromium. Every result is an <a data-impression-key="{package_id}"> card in the page.
Search cards carry no developer, so each brand match's detail page is read in the same browser for its developer.

Search URL: https://www.phonepeappstore.com/search/?ts={query}
App page:   https://www.phonepeappstore.com/apps/{category}/{slug}/{package_id}/?page=details&id={package_id}
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
from urllib.parse import quote_plus, urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.api.models import (
    Hit,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.services.keyword_expansion import fold_homoglyphs
from app.sources.base import SourceAdapter, brand_words, pkg_queries, scrape_status, scraped_hit
from app.utils.browser import browser_context, page_html
from app.utils.http_client import FetchError, throttle

log = logging.getLogger(__name__)

_BASE = "https://www.phonepeappstore.com"
_SEARCH = _BASE + "/search/?ts={q}"

# Result cards: <a href="/apps/<category>/<slug>/<pkg>/?page=details&id=<pkg>" data-impression-key="<pkg>">
_CARD_SEL = 'a[href^="/apps/"][data-impression-key]'

# Detail pages embed Astro island props, e.g. developerDisplayName&quot;:[0,&quot;PhonePe&quot;]
_DEVELOPER_RE = re.compile(r'"developerDisplayName":\[0,"((?:[^"\\]|\\.)*)"\]')


def _developer_from_detail(page_html: str) -> str | None:
    m = _DEVELOPER_RE.search(html.unescape(page_html))
    return html.unescape(m.group(1)).strip() or None if m else None


def parse_search_results(page_html: str) -> list[dict]:
    """Extract app cards from a search-results page.

    Returns dicts with: package_id, name, url, icon_url, rating, category.
    """
    tree = HTMLParser(page_html)
    out: list[dict] = []
    seen: set[str] = set()
    for a in tree.css(_CARD_SEL):
        pkg_id = (a.attributes.get("data-impression-key") or "").strip()
        href = a.attributes.get("href") or ""
        if not pkg_id or pkg_id in seen:
            continue
        seen.add(pkg_id)

        name_el = a.css_first("p")
        img_el = a.css_first("img")
        rating_el = a.css_first("span")

        name = name_el.text(strip=True) if name_el else ""
        if not name and img_el:
            # alt is "<name> | PhonePe Appstore | App Icon"
            name = (img_el.attributes.get("alt") or "").split(" | ")[0].strip()
        if not name:
            name = pkg_id.split(".")[-1].replace("_", " ").title()

        parts = href.split("?")[0].strip("/").split("/")  # apps/<category>/<slug>/<pkg>
        out.append(
            {
                "package_id": pkg_id,
                "name": name,
                "url": urljoin(_BASE, href),
                "icon_url": (img_el.attributes.get("src") if img_el else None) or None,
                "rating": rating_el.text(strip=True) if rating_el else None,
                "category": parts[1] if len(parts) >= 4 else None,
            }
        )
    return out


class IndusAppstoreAdapter(SourceAdapter):
    name = "indus_appstore"
    method = SourceMethod.scrape
    host_group = "app_store"
    max_results = 10

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        brand = ctx.request.brand_name
        brand_tokens = brand_words(brand)

        hits: list[Hit] = []
        seen: set[str] = set()
        errors: list[str] = []
        try:
            async with browser_context() as bctx:
                for q in pkg_queries(ctx)[:2]:
                    search_url = _SEARCH.format(q=quote_plus(q))
                    try:
                        await throttle(search_url)
                        cards = parse_search_results(await page_html(bctx, search_url))
                    except Exception as e:
                        log.warning("indus: search failed for query %r: %s", q, e)
                        errors.append(f"{type(e).__name__}: {e}")
                        continue
                    log.debug("indus: %d cards for query %r", len(cards), q)

                    matched: list[dict] = []
                    for card in cards:
                        name_lower = fold_homoglyphs(card["name"]).lower()
                        pkg_lower = card["package_id"].lower()
                        if brand_tokens and not (
                            any(tok in name_lower for tok in brand_tokens)
                            or any(tok in pkg_lower for tok in brand_tokens)
                        ):
                            continue
                        uid = hashlib.sha256(card["url"].encode()).hexdigest()[:32]
                        if uid in seen:
                            continue
                        seen.add(uid)
                        matched.append({**card, "uid": uid})

                    publishers = await asyncio.gather(
                        *(
                            self._fetch_publisher(bctx, c["url"])
                            for c in matched[: self.max_results]
                        )
                    )
                    for card, publisher in zip(matched, publishers, strict=False):
                        hits.append(
                            scraped_hit(
                                self,
                                q,
                                url=card["url"],
                                host_name="PhonePe Appstore",
                                host_domain="phonepeappstore.com",
                                hit_id=card["uid"],
                                app_name=card["name"],
                                package_id=card["package_id"],
                                publisher=publisher,
                                icon_url=card["icon_url"],
                            )
                        )
        except FetchError as e:  # Playwright not installed: the source is unavailable
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed", error=str(e)
            )

        return hits, scrape_status(self.name, self.method, hits, errors=errors, blocked=[])

    @staticmethod
    async def _fetch_publisher(context, url: str) -> str | None:
        try:
            await throttle(url)
            return _developer_from_detail(await page_html(context, url))
        except Exception as e:
            log.debug("indus: detail read failed for %s: %s", url, e)
            return None
