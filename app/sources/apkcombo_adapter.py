"""APKCombo adapter — HTML scraper.

Search URL: https://apkcombo.com/search/{query}/
Package IDs are embedded in listing hrefs: /apk/{slug}/{package.name}/
Auth: None
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import re
from urllib.parse import quote_plus

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
from app.utils.browser import hits_with_browser_fallback
from app.utils.http_client import BlockedError, FetchError

log = logging.getLogger(__name__)

_BASE = "https://apkcombo.com"
_SEARCH = _BASE + "/search/{q}/"

_PKG_RE = re.compile(r'/([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]+)+)/', re.IGNORECASE)

_CARD_SELECTORS = [".search-app-item", ".app-item", "li.search-item", "article"]
_NAME_SELECTORS = ["h3", "h4", ".app-name", ".name", ".title"]
_DEV_SELECTORS = [".developer", ".by", ".author", ".publisher"]


class APKComboAdapter(SourceAdapter):
    name = "apkcombo"
    method = SourceMethod.scrape
    host_group = "apk_site"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        brand = ctx.request.brand_name
        brand_tokens = {t.lower() for t in fold_homoglyphs(brand).split() if len(t) >= 3}

        queries = pkg_queries(ctx)

        hits: list[Hit] = []
        seen: set[str] = set()
        error: str | None = None

        for q in queries[:2]:
            url = _SEARCH.format(q=quote_plus(q))
            try:
                hits.extend(await hits_with_browser_fallback(
                    url,
                    lambda html, q=q: self._parse(html, q, brand_tokens, seen),
                    rotate_ua=True,
                ))
            except BlockedError as e:
                return hits, SourceStatus(
                    name=self.name, method=self.method, status="blocked", error=str(e)
                )
            except FetchError as e:
                error = str(e)
                log.warning("apkcombo: fetch failed for %r: %s", q, e)
                continue

        return hits, SourceStatus(
            name=self.name, method=self.method,
            status="ok" if not error else "failed",
            results=len(hits),
            error=error if not hits else None,
        )

    def _parse(self, html: str, query: str, brand_tokens: set[str], seen: set[str]) -> list[Hit]:
        tree = HTMLParser(html)
        hits: list[Hit] = []

        # Try each card selector
        cards = []
        for sel in _CARD_SELECTORS:
            cards = tree.css(sel)
            if cards:
                break

        # Fallback: all links to /apk/ or app listing paths
        if not cards:
            cards = tree.css("a[href*='/apk/']") or tree.css("a[href*='/en/']")

        for card in cards[:30]:
            # Find a link inside the card (or the card itself if it's an <a>)
            a = card.css_first("a") if card.tag != "a" else card
            if not a:
                continue
            href = (a.attributes.get("href") or "").strip()
            if not href:
                continue

            full_url = href if href.startswith("http") else f"{_BASE}{href}"
            if "apkcombo.com" not in full_url:
                continue

            # Package from URL
            pkg_id: str | None = None
            m = _PKG_RE.search(href)
            if m:
                pkg_id = m.group(1)

            # App name
            name: str | None = None
            for sel in _NAME_SELECTORS:
                node = card.css_first(sel)
                if node:
                    name = node.text(strip=True) or None
                    if name:
                        break
            if not name:
                name = a.text(strip=True) or None
            if not name and pkg_id:
                name = pkg_id

            if not name:
                continue

            name_lower = fold_homoglyphs(name).lower()
            pkg_lower = (pkg_id or "").lower()
            if brand_tokens and not any(tok in name_lower or tok in pkg_lower for tok in brand_tokens):
                continue

            uid = hashlib.sha256(full_url.encode()).hexdigest()[:32]
            if uid in seen:
                continue
            seen.add(uid)

            publisher: str | None = None
            for sel in _DEV_SELECTORS:
                node = card.css_first(sel)
                if node:
                    publisher = node.text(strip=True) or None
                    if publisher:
                        break

            hits.append(Hit(
                id=uid,
                type=HitType.app_listing,
                app_name=name,
                package_id=pkg_id,
                publisher=publisher,
                url=full_url,
                host=HostInfo(
                    group=self.host_group,
                    name="APKCombo",
                    domain="apkcombo.com",
                    is_official_store=False,
                ),
                found_via=[FoundVia(source=self.name, method=self.method, query=query)],
                relevance_score=0,
                discovered_at=datetime.datetime.now(datetime.timezone.utc),
            ))

        return hits
