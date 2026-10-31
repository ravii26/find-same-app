"""PhonePe Appstore (formerly Indus Appstore) adapter — static HTTP scraper.

Indus Appstore is live on 100M+ devices in India (pre-installed on Xiaomi,
Redmi, Poco, Lava, and Motorola India). It is a P1 Critical source for
India-focused rogue app monitoring.

The search page is server-rendered (Astro), so no browser is needed: every
result is an <a data-impression-key="{package_id}"> card in the initial HTML.

Search URL: https://www.phonepeappstore.com/search/?ts={query}
App page:   https://www.phonepeappstore.com/apps/{category}/{slug}/{package_id}/?page=details&id={package_id}
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import html
import logging
import re
from urllib.parse import quote_plus, urljoin

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser

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
from app.utils.browser import render_html
from app.utils.http_client import throttle

log = logging.getLogger(__name__)

_BASE = "https://www.phonepeappstore.com"
_SEARCH = _BASE + "/search/?ts={q}"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

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
        out.append({
            "package_id": pkg_id,
            "name": name,
            "url": urljoin(_BASE, href),
            "icon_url": (img_el.attributes.get("src") if img_el else None) or None,
            "rating": rating_el.text(strip=True) if rating_el else None,
            "category": parts[1] if len(parts) >= 4 else None,
        })
    return out


class IndusAppstoreAdapter(SourceAdapter):
    name = "indus_appstore"
    method = SourceMethod.scrape
    host_group = "app_store"
    max_results = 10

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        brand = ctx.request.brand_name
        brand_tokens = {t.lower() for t in fold_homoglyphs(brand).split() if len(t) >= 3}

        hits: list[Hit] = []
        seen: set[str] = set()
        errors: list[str] = []

        async with httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"},
        ) as client:
            for q in pkg_queries(ctx)[:2]:
                search_url = _SEARCH.format(q=quote_plus(q))
                cards: list[dict] = []
                try:
                    await throttle(search_url)
                    resp = await client.get(search_url)
                    resp.raise_for_status()
                    cards = parse_search_results(resp.text)
                except Exception as e:
                    log.warning("indus: search failed for query %r: %s", q, e)
                    errors.append(f"{type(e).__name__}: {e}")

                if not cards:
                    # httpx gave no cards (error or empty page): try the rendered page before giving up
                    cards = await self._browser_cards(search_url)
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
                    card["uid"] = uid
                    matched.append(card)

                # Search cards carry no developer — fetch it from the detail page
                # (only for brand matches, so this stays a handful of requests).
                publishers = await asyncio.gather(
                    *(self._fetch_publisher(client, c["url"]) for c in matched[: self.max_results])
                )

                for card, publisher in zip(matched, publishers):
                    hits.append(Hit(
                        id=card["uid"],
                        type=HitType.app_listing,
                        app_name=card["name"],
                        package_id=card["package_id"],
                        publisher=publisher,
                        icon_url=card["icon_url"],
                        url=card["url"],
                        host=HostInfo(
                            group=self.host_group,
                            name="PhonePe Appstore",
                            domain="phonepeappstore.com",
                            is_official_store=False,
                        ),
                        found_via=[FoundVia(source=self.name, method=self.method, query=q)],
                        relevance_score=0,
                        discovered_at=datetime.datetime.now(datetime.timezone.utc),
                    ))

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

    @staticmethod
    async def _browser_cards(search_url: str) -> list[dict]:
        """Search cards from the rendered page, for when the httpx request gave none."""
        try:
            html = await render_html(search_url)
        except Exception as e:  # Playwright missing or browser failed: keep the httpx outcome
            log.warning("indus: browser fallback failed for %s: %s", search_url, e)
            return []
        return parse_search_results(html)

    @staticmethod
    async def _fetch_publisher(client: httpx.AsyncClient, url: str) -> str | None:
        try:
            await throttle(url)
            resp = await client.get(url)
            resp.raise_for_status()
            return _developer_from_detail(resp.text)
        except Exception as e:
            log.debug("indus: detail fetch failed for %s: %s", url, e)
            return None
