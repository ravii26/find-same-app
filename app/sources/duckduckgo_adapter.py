"""DuckDuckGo HTML search adapter.

Endpoint: https://html.duckduckgo.com/html/?q={query}, rendered in Chromium (Playwright).
Auth: None. A challenge page (captcha or cookie check) with no results is reported as blocked.
"""

from __future__ import annotations

import hashlib
import logging
from urllib.parse import quote_plus

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.api.models import Hit, HitType, ScanContext, SourceMethod, SourceStatus
from app.sources.base import SourceAdapter, brand_words, page_problem, scrape_status, scraped_hit
from app.utils.browser import render_html

log = logging.getLogger(__name__)

_DDG_URL = "https://html.duckduckgo.com/html/"


class DuckDuckGoAdapter(SourceAdapter):
    name = "duckduckgo"
    method = SourceMethod.scrape
    host_group = "web_search"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        brand = ctx.request.brand_name
        pkg = ctx.request.android_package or ""
        brand_tokens = brand_words(brand)

        queries: list[str] = [f"{brand} apk"]
        if pkg:
            # Use only the last package component to avoid overly specific queries
            last_component = pkg.split(".")[-1]
            if last_component.lower() != brand.lower():
                queries.append(f"{last_component} apk")
        for kw in ctx.expanded_keywords:
            if any(tok in kw.term.lower() for tok in brand_tokens):
                candidate = f"{kw.term} apk"
                if candidate not in queries:
                    queries.append(candidate)
                    break

        hits: list[Hit] = []
        seen: set[str] = set()
        errors: list[str] = []
        blocked: list[str] = []
        for q in queries[:3]:
            try:
                html = await render_html(f"{_DDG_URL}?q={quote_plus(q)}")
            except Exception as e:
                log.warning("duckduckgo: browser failed for %r: %s", q, e)
                errors.append(f"{type(e).__name__}: {e}")
                continue
            new_hits = self._parse(html, q, seen)
            if not new_hits and (problem := page_problem(None, "", html)):
                log.warning("duckduckgo: blocked for query %r: %s", q, problem)
                blocked.append(problem)
                continue
            hits.extend(new_hits)

        return hits, scrape_status(self.name, self.method, hits, errors=errors, blocked=blocked)

    def _parse(self, html: str, query: str, seen: set[str]) -> list[Hit]:
        tree = HTMLParser(html)
        hits: list[Hit] = []

        for result in tree.css(".result"):
            a = result.css_first("a.result__a")
            if not a:
                continue
            url = a.attributes.get("href", "").strip()
            if not url or not url.startswith("http"):
                continue

            uid = hashlib.sha256(url.encode()).hexdigest()[:32]
            if uid in seen:
                continue
            seen.add(uid)

            hits.append(
                scraped_hit(
                    self,
                    query,
                    url=url,
                    host_name="DuckDuckGo",
                    host_domain="duckduckgo.com",
                    hit_id=uid,
                    hit_type=HitType.web_page,
                    title=a.text(strip=True) or None,
                )
            )

        return hits
