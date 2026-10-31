"""APKKit adapter — search-page scraper.

Search URL: https://apkkit.com/en/home/search?q={query}   (robots.txt: Allow: /)
Results are filled in by a Google Custom Search widget, so httpx alone returns no result links;
hits_with_browser_fallback() renders the page in Chromium for those. Only the result titles are read
(a.gs-title). Category (/c/) and developer (/dev/) pages are skipped.
App URLs are slugs (/en/<slug>) with no package id, so package_id stays empty.
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

_BASE = "https://apkkit.com"
_SEARCH = _BASE + "/en/home/search?q={q}"

_NON_APP_RE = re.compile(r"/(c|dev|tag|tags)/", re.IGNORECASE)


class APKKitAdapter(SourceAdapter):
    name = "apkkit"
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
                    rotate_ua=True, browser_wait_for="a.gs-title",
                ))
            except BlockedError as e:
                return hits, SourceStatus(
                    name=self.name, method=self.method, status="blocked", error=str(e)
                )
            except FetchError as e:
                error = str(e)
                log.warning("apkkit: fetch failed for %r: %s", q, e)
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

        for a in tree.css("a.gs-title"):
            href = (a.attributes.get("href") or "").strip()
            if not href or _NON_APP_RE.search(href):
                continue
            full_url = href if href.startswith("http") else f"{_BASE}{href}"

            raw = re.sub(r"\s+", " ", a.text(deep=True, separator=" ", strip=True))
            name = re.sub(r"\s*-\s*APKKIT\s*$", "", raw, flags=re.IGNORECASE).strip()
            if not name:
                continue

            name_lower = fold_homoglyphs(name).lower()
            if brand_tokens and not any(tok in name_lower for tok in brand_tokens):
                continue

            uid = hashlib.sha256(full_url.encode()).hexdigest()[:32]
            if uid in seen:
                continue
            seen.add(uid)

            hits.append(Hit(
                id=uid,
                type=HitType.app_listing,
                app_name=name,
                package_id=None,
                publisher=None,
                url=full_url,
                host=HostInfo(
                    group=self.host_group,
                    name="APKKit",
                    domain="apkkit.com",
                    is_official_store=False,
                ),
                found_via=[FoundVia(source=self.name, method=self.method, query=query)],
                relevance_score=0,
                discovered_at=datetime.datetime.now(datetime.timezone.utc),
            ))

        return hits
