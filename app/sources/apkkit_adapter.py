"""APKKit adapter — search-page scraper.

Search URL: https://apkkit.com/en/home/search?q={query}   (robots.txt: Allow: /)
Results are filled in by a Google Custom Search widget, so httpx alone returns no result links;
hits_from_browser() renders the page in Chromium for those. Only the result titles are read
(a.gs-title). Category (/c/) and developer (/dev/) pages are skipped.
App URLs are slugs (/en/<slug>) with no package id, so package_id stays empty.
"""

from __future__ import annotations

import hashlib
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
from app.services.brand_match import presence_any
from app.sources.apk_detail import parse_app_page
from app.sources.base import SourceAdapter, pkg_queries, scraped_hit
from app.utils.browser import hits_from_browser
from app.utils.http_client import BlockedError, FetchError, decode_response, fetch

log = logging.getLogger(__name__)

_BASE = "https://apkkit.com"
_SEARCH = _BASE + "/en/home/search?q={q}"

_NON_APP_RE = re.compile(r"/(c|dev|tag|tags)/", re.IGNORECASE)
# App pages read per scan: one plain HTTP request each, on top of the search pages.
_APP_PAGE_LIMIT = 10
# App page field -> Hit field. The page title (h1) becomes the hit's title; the name is taken from it below.
_PAGE_FIELDS = {
    "title": "title",
    "developer": "publisher",
    "version": "version",
}
# The app page title is "Download <name> APK <version> for Android <year>"; the name is what sits between.
_TITLE_RE = re.compile(r"^(?:download\s+)?(.+?)\s+(?:apk\b|for\s+android\b)", re.IGNORECASE)


def _app_name(title: str) -> str:
    m = _TITLE_RE.match(title.strip())
    return m.group(1).strip() if m else title.strip()


class APKKitAdapter(SourceAdapter):
    name = "apkkit"
    method = SourceMethod.scrape
    host_group = "apk_site"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        brand = ctx.request.brand_name
        brands = [brand, *ctx.request.brand_aliases]

        queries = pkg_queries(ctx)

        hits: list[Hit] = []
        seen: set[str] = set()
        error: str | None = None

        for q in queries[:2]:
            url = _SEARCH.format(q=quote_plus(q))
            try:
                hits.extend(await hits_from_browser(
                    url,
                    lambda html, q=q: self._parse(html, q, brands, seen),
                    wait_for_selector="a.gs-title",
                ))
            except BlockedError as e:
                return hits, SourceStatus(
                    name=self.name, method=self.method, status="blocked", error=str(e)
                )
            except FetchError as e:
                error = str(e)
                log.warning("apkkit: fetch failed for %r: %s", q, e)
                continue

        # The search card only has a link title. The app's own page lists the developer, version and name.
        hits = [await self._with_app_page(h) for h in hits[:_APP_PAGE_LIMIT]] + hits[_APP_PAGE_LIMIT:]

        return hits, SourceStatus(
            name=self.name, method=self.method,
            status="ok" if not error else "failed",
            results=len(hits),
            error=error if not hits else None,
        )

    async def _with_app_page(self, hit: Hit) -> Hit:
        """The hit with the facts from its app page. A page that cannot be read keeps the search card's fields."""
        try:
            response = await fetch(hit.url)
        except (BlockedError, FetchError) as e:
            log.info("apkkit: app page not read %s: %s", hit.url, e)
            return hit
        fields = parse_app_page(decode_response(response))
        if not fields:
            return hit
        update: dict[str, str] = {
            dst: fields[src] for src, dst in _PAGE_FIELDS.items() if fields.get(src)
        }
        if fields.get("title"):
            update["app_name"] = _app_name(fields["title"])
        if fields.get("icon"):
            update["icon_url"] = urljoin(hit.url, fields["icon"])
        return hit.model_copy(update=update)

    def _parse(self, html: str, query: str, brands: list[str], seen: set[str]) -> list[Hit]:
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

            if presence_any(brands, name, None, None) is None:  # brand or a near-miss spelling, as the scorer reads it
                continue

            uid = hashlib.sha256(full_url.encode()).hexdigest()[:32]
            if uid in seen:
                continue
            seen.add(uid)

            hits.append(scraped_hit(
                self,
                query,
                url=full_url,
                host_name="APKKit",
                host_domain="apkkit.com",
                hit_id=uid,
                app_name=name,
            ))

        return hits
