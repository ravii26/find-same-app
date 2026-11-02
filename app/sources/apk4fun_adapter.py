"""APK4Fun adapter — search-page scraper.

Search URL: https://www.apk4fun.com/s.php?s={query}   (robots.txt allows /s.php)
The results are filled in by a Google Custom Search widget, so httpx alone returns no result links;
hits_from_browser() renders the page in Chromium for those. Only the result titles are read
(a.gs-title); the sidebar's "Popular Games" links are ignored.
Package IDs appear in /apps/{package}/ URLs. /apk/{id}/ URLs carry no package id.
"""

from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import quote_plus

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.api.models import (
    Hit,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.services.brand_match import presence_any
from app.sources.base import SourceAdapter, pkg_queries, scraped_hit
from app.utils.browser import hits_from_browser
from app.utils.http_client import BlockedError, FetchError, decode_response, fetch, throttle

log = logging.getLogger(__name__)

_BASE = "https://www.apk4fun.com"
_SEARCH = _BASE + "/s.php?s={q}"

_PKG_RE = re.compile(r"/apps/([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)/", re.IGNORECASE)
# App pages read per scan: one plain HTTP request each (the /apps/<package>/ pages; robots.txt allows them).
_APP_PAGE_LIMIT = 10


_VERSION_RE = re.compile(r"Latest Version:\s*([^\n•]+)")
_DEVELOPER_RE = re.compile(r"Developer:\s*([^\n•]+)")
_DESC_END_RE = re.compile(r"\n\s*(Permissions|Related|Download)\b")


def app_facts(page_html: str) -> dict[str, str]:
    """Version, developer and description from an /apps/<package>/ page (text labels, then the description block)."""
    tree = HTMLParser(page_html)
    lines_text = tree.body.text(separator="\n", strip=True) if tree.body else ""
    facts: dict[str, str] = {}
    if m := _VERSION_RE.search(lines_text):
        facts["version"] = m.group(1).strip()
    if m := _DEVELOPER_RE.search(lines_text):
        facts["developer"] = m.group(1).strip()
    # The description block starts "<App> Description: <text>"; the smallest element that holds that label
    best = None
    for node in tree.css("article, div, section, p"):
        tx = node.text(strip=True)
        if " Description:" in tx[:60] or tx.startswith("Description:"):
            if best is None or len(tx) < len(best):
                best = tx
    if best:
        text = best.split("Description:", 1)[1].strip()
        end = _DESC_END_RE.search(text)
        text = (text[: end.start()] if end else text).strip()
        if text:
            facts["description"] = text[:1000]  # same cap as the other stores
    return facts


class APK4FunAdapter(SourceAdapter):
    name = "apk4fun"
    method = SourceMethod.scrape
    host_group = "apk_site"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        brands = [ctx.request.brand_name, *ctx.request.brand_aliases]

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
                log.warning("apk4fun: fetch failed for %r: %s", q, e)
                continue

        # The search card has the name and package only. Each package's app page gives version, developer and description.
        hits = [await self._with_app_page(h) for h in hits[:_APP_PAGE_LIMIT]] + hits[_APP_PAGE_LIMIT:]

        return hits, SourceStatus(
            name=self.name, method=self.method,
            status="ok" if not error else "failed",
            results=len(hits),
            error=error if not hits else None,
        )

    @staticmethod
    async def _with_app_page(hit: Hit) -> Hit:
        """The hit with version, developer and description from its /apps/<package>/ page. A page that cannot be read keeps the search fields."""
        if not hit.package_id or "/apps/" not in hit.url:
            return hit
        try:
            await throttle(hit.url)
            resp = await fetch(hit.url, rotate_ua=False)
        except (BlockedError, FetchError) as e:
            log.info("apk4fun: app page not read %s: %s", hit.url, e)
            return hit
        facts = app_facts(decode_response(resp))
        update = {dst: facts[src] for src, dst in (("version", "version"), ("developer", "publisher"), ("description", "description")) if facts.get(src)}
        return hit.model_copy(update=update) if update else hit

    def _parse(self, html: str, query: str, brands: list[str], seen: set[str]) -> list[Hit]:
        tree = HTMLParser(html)
        hits: list[Hit] = []

        for a in tree.css("a.gs-title"):
            href = (a.attributes.get("href") or "").strip()
            if "/apk/" not in href and "/apps/" not in href:
                continue
            full_url = href if href.startswith("http") else f"{_BASE}{href}"

            # Title is "<App> APK v1.2 - APK4Fun"; drop the site suffix
            raw = re.sub(r"\s+", " ", a.text(deep=True, separator=" ", strip=True))
            name = re.sub(r"\s*-\s*APK4Fun(\.com)?\s*$", "", raw, flags=re.IGNORECASE).strip()
            if not name:
                continue

            pkg_id: str | None = None
            m = _PKG_RE.search(href)
            if m:
                pkg_id = m.group(1)

            # Keep only apps that carry the brand, near misses included (the same test the scorer uses)
            if presence_any(brands, name, pkg_id, None) is None:
                continue

            uid = hashlib.sha256(full_url.encode()).hexdigest()[:32]
            if uid in seen:
                continue
            seen.add(uid)

            hits.append(scraped_hit(
                self,
                query,
                url=full_url,
                host_name="APK4Fun",
                host_domain="apk4fun.com",
                hit_id=uid,
                app_name=name,
                package_id=pkg_id,
            ))

        return hits
