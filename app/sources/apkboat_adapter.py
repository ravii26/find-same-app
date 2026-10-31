"""APKBoat adapter — search-page scraper (plain HTTP, no browser).

Search URL: https://apkboat.io/?s={query}   (robots.txt: no Disallow rules)
Results are in the static HTML as cards: a.archive-post with the app page URL in href and the app name in h3.
Only metadata is read. No APK file is fetched; APK analysis is off (see analyze_apks).
App URLs are slugs (/<slug>/) with no package id, so package_id stays empty.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
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
from app.utils.http_client import BlockedError, FetchError, decode_response, fetch

log = logging.getLogger(__name__)

_BASE = "https://apkboat.io"
_SEARCH = _BASE + "/?s={q}"


class APKBoatAdapter(SourceAdapter):
    name = "apkboat"
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
                html = decode_response(await fetch(url, rotate_ua=True, retries=1))
            except BlockedError as e:
                return hits, SourceStatus(
                    name=self.name, method=self.method, status="blocked", error=str(e)
                )
            except FetchError as e:
                error = str(e)
                log.warning("apkboat: fetch failed for %r: %s", q, e)
                continue
            hits.extend(self._parse(html, q, brand_tokens, seen))

        return hits, SourceStatus(
            name=self.name, method=self.method,
            status="ok" if not error else "failed",
            results=len(hits),
            error=error if not hits else None,
        )

    def _parse(self, html: str, query: str, brand_tokens: set[str], seen: set[str]) -> list[Hit]:
        tree = HTMLParser(html)
        hits: list[Hit] = []

        for card in tree.css("a.archive-post"):
            href = (card.attributes.get("href") or "").strip()
            if not href.startswith(_BASE):
                continue

            h3 = card.css_first("h3")
            name = (h3.text(strip=True) if h3 else "") or (card.attributes.get("title") or "").strip()
            if not name:
                continue

            name_lower = fold_homoglyphs(name).lower()
            if brand_tokens and not any(tok in name_lower for tok in brand_tokens):
                continue

            uid = hashlib.sha256(href.encode()).hexdigest()[:32]
            if uid in seen:
                continue
            seen.add(uid)

            hits.append(Hit(
                id=uid,
                type=HitType.app_listing,
                app_name=name,
                package_id=None,
                publisher=None,
                url=href,
                host=HostInfo(
                    group=self.host_group,
                    name="APKBoat",
                    domain="apkboat.io",
                    is_official_store=False,
                ),
                found_via=[FoundVia(source=self.name, method=self.method, query=query)],
                relevance_score=0,
                discovered_at=datetime.datetime.now(datetime.timezone.utc),
            ))

        return hits
