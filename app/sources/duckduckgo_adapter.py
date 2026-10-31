"""DuckDuckGo HTML search adapter.

Endpoint: POST https://html.duckduckgo.com/html/  (form field: q)
Auth: None — server-rendered HTML, no API key required.

DDG uses a cookie-challenge flow: the first POST returns 202 and sets a vqd
cookie; only the second POST (with that cookie) returns actual results.
A dedicated per-search httpx client is used so the DDG cookie is isolated
from the shared client used by all other adapters.
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import random
from urllib.parse import quote_plus

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
from app.sources.base import SourceAdapter
from app.utils.browser import render_html
from app.utils.http_client import _USER_AGENTS, throttle  # UA pool; rate limit shared with fetch()

log = logging.getLogger(__name__)

_DDG_URL = "https://html.duckduckgo.com/html/"

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
    "Origin": "https://html.duckduckgo.com",
    "Referer": "https://html.duckduckgo.com/html/",
}


async def _ddg_post(client: httpx.AsyncClient, query: str) -> str:
    """POST to DDG and return response HTML. Handles the 202 cookie challenge
    by retrying once with the cookie that DDG set on the first response."""
    data = {"q": query, "kl": "us-en"}
    resp = await client.post(_DDG_URL, data=data)
    if resp.status_code == 202:
        resp = await client.post(_DDG_URL, data=data)
    if resp.status_code == 202:
        # Second 202 means DDG is serving a CAPTCHA challenge — IP is flagged
        raise httpx.HTTPStatusError(
            "DDG CAPTCHA challenge (IP blocked)", request=resp.request, response=resp
        )
    if resp.status_code not in (200,):
        raise httpx.HTTPStatusError(
            f"DDG returned {resp.status_code}", request=resp.request, response=resp
        )
    return resp.text


class DuckDuckGoAdapter(SourceAdapter):
    name = "duckduckgo"
    method = SourceMethod.scrape
    host_group = "web_search"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        brand = ctx.request.brand_name
        pkg = ctx.request.android_package or ""
        brand_tokens = {t.lower() for t in fold_homoglyphs(brand).split() if len(t) >= 3}

        queries: list[str] = [f"{brand} apk"]
        if pkg:
            # Use only the last package component to avoid overly specific queries
            last_component = pkg.split(".")[-1]
            if last_component.lower() != brand.lower():
                queries.append(f"{last_component} apk")
        for kw in ctx.expanded_keywords:
            if any(tok in kw.term.lower() for tok in brand_tokens):
                term = kw.term
                candidate = f"{term} apk"
                if candidate not in queries:
                    queries.append(candidate)
                    break

        hits: list[Hit] = []
        seen: set[str] = set()
        error: str | None = None

        # Dedicated client per search — keeps DDG cookies isolated
        headers = {**_HEADERS, "User-Agent": random.choice(_USER_AGENTS)}
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(15.0),
            headers=headers,
        ) as client:
            for q in queries[:3]:
                blocked: str | None = None
                new_hits: list[Hit] = []
                try:
                    await throttle(_DDG_URL)
                    html = await _ddg_post(client, q)
                    new_hits = self._parse(html, q, seen)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code in (202, 403, 429):
                        log.warning("duckduckgo: blocked for query %r: %s", q, e)
                        blocked = str(e)
                    else:
                        error = str(e)
                        log.warning("duckduckgo: request failed for query %r: %s", q, e)
                        continue
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"
                    log.warning("duckduckgo: request failed for query %r: %s", q, e)
                    continue

                if not new_hits:
                    new_hits = await self._browser_hits(q, seen)  # browser only when httpx gave nothing
                if blocked and not new_hits:
                    return hits, SourceStatus(
                        name=self.name, method=self.method,
                        status="blocked", error=blocked,
                    )
                hits.extend(new_hits)

        if hits:
            status = "ok"
        elif error and "403" in str(error):
            status = "blocked"
        elif error:
            status = "failed"
        else:
            status = "ok"
        return hits, SourceStatus(
            name=self.name,
            method=self.method,
            status=status,
            results=len(hits),
            error=error if not hits else None,
        )

    async def _browser_hits(self, query: str, seen: set[str]) -> list[Hit]:
        """Same search in a real browser, for when the httpx POST returned no results."""
        try:
            html = await render_html(f"{_DDG_URL}?q={quote_plus(query)}")
        except Exception as e:  # Playwright missing or browser failed: keep the httpx outcome
            log.warning("duckduckgo: browser fallback failed for %r: %s", query, e)
            return []
        return self._parse(html, query, seen)

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

            title = a.text(strip=True) or None

            hits.append(
                Hit(
                    id=uid,
                    type=HitType.web_page,
                    title=title,
                    url=url,
                    host=HostInfo(
                        group=self.host_group,
                        name="DuckDuckGo",
                        domain="duckduckgo.com",
                        is_official_store=False,
                    ),
                    found_via=[
                        FoundVia(source=self.name, method=self.method, query=query)
                    ],
                    relevance_score=0,
                    discovered_at=datetime.datetime.now(datetime.timezone.utc),
                )
            )

        return hits
