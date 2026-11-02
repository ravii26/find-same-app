# app/sources/bing_adapter.py
"""Bing web search adapter.

Endpoint: https://www.bing.com/search?q={query}&setlang=en&cc={country}
Auth: None — public HTML search.
Rate limit: conservative (1-2 req/s).

Bing is the web search used to find APK download pages and mirrors.
Finds APK download pages, unofficial mirrors, and rogue app listings across the web.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import logging
import re
import time
from urllib.parse import parse_qs, quote_plus, urlparse

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
from app.config import phrases
from app.sources.base import SourceAdapter, brand_words, country_label, scan_countries
from app.utils.browser import hits_from_browser
from app.utils.http_client import BlockedError, FetchError

log = logging.getLogger(__name__)

_SEARCH = "https://www.bing.com/search?q={q}&setlang=en&cc={cc}"  # cc = market (changes the results)
_BING_REDIRECT_RE = re.compile(r"[&?]u=a1([A-Za-z0-9_-]+)")
_SKIP_DOMAINS = {"bing.com", "microsoft.com", "msn.com"}


def _extract_url(href: str, cite_text: str) -> str | None:
    """Extract real destination URL from a Bing redirect href."""
    m = _BING_REDIRECT_RE.search(href)
    if m:
        padded = m.group(1) + "=="
        try:
            return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace").strip()
        except Exception as exc:
            log.warning("bing: could not decode result link %r: %s", href[:120], exc)
    # Fall back to cite text — strip › separators to reconstruct URL
    cite = cite_text.strip()
    if cite and "." in cite:
        cite = cite.replace(" › ", "/")
        if not cite.startswith("http"):
            cite = "https://" + cite
        return cite
    return None


class BingAdapter(SourceAdapter):
    name = "bing"
    method = SourceMethod.scrape
    host_group = "search_engine"

    def _make_host(self, real_url: str) -> HostInfo:
        """Host recorded on each hit. Subclasses override to record the real host."""
        return HostInfo(group=self.host_group, name="Bing", domain="bing.com", is_official_store=False)

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        brand = ctx.request.brand_name
        brand_tokens = brand_words(brand)
        official_domain = (ctx.request.official_domain or "").lower().strip()

        skip_domains = set(_SKIP_DOMAINS)
        if official_domain:
            skip_domains.add(official_domain)
            # Also skip www. variant
            skip_domains.add(f"www.{official_domain}")

        queries: list[str] = [f"{brand} apk", f"{brand} apk download"]
        for kw in ctx.expanded_keywords:
            term = kw.term.lower()
            if any(tok in term for tok in brand_tokens) and term not in (q.lower() for q in queries):
                queries.append(f"{kw.term} apk")
                break

        hits: list[Hit] = []
        seen: set[str] = set()
        error: str | None = None

        countries = scan_countries(ctx)
        # English queries for every country, plus the localized ones of that country's market (config/phrases.yaml).
        # Ordered query by query across all countries, so a run cut short by its time budget still covered every
        # market with the most important queries.
        per_country = {cc: queries[:3] + [t.format(brand=brand) for t in phrases.search_queries(cc)] for cc in countries}
        depth = max((len(qs) for qs in per_country.values()), default=0)
        plan = [(cc, per_country[cc][i]) for i in range(depth) for cc in countries if i < len(per_country[cc])]

        # Several scans at once share Bing's per-site rate limit, so a full plan can outlast the source time limit,
        # which would discard everything found. Stop starting queries at 75% of it and report what was found.
        from app.config.settings import get_settings
        budget = 0.75 * get_settings().source_timeout_seconds
        started = time.monotonic()
        done = 0
        for cc, q in plan:
            if time.monotonic() - started > budget:
                error = (f"time budget reached: ran {done} of {len(plan)} queries (scans running at once share "
                         "Bing's rate limit)")
                log.warning("bing: %s", error)
                break
            done += 1
            url = _SEARCH.format(q=quote_plus(q), cc=cc)
            label = country_label(q, cc, countries)
            remaining = budget - (time.monotonic() - started)
            try:
                # The budget also bounds the query in progress: under load one query can wait a minute for its turn
                hits.extend(await asyncio.wait_for(hits_from_browser(
                    url,
                    lambda html, label=label: self._parse(html, label, brand_tokens, skip_domains, seen),
                ), timeout=max(remaining, 1.0)))
            except asyncio.TimeoutError:
                error = (f"time budget reached: ran {done - 1} of {len(plan)} queries (scans running at once share "
                         "Bing's rate limit)")
                log.warning("bing: %s", error)
                break
            except BlockedError as e:
                return hits, SourceStatus(
                    name=self.name, method=self.method, status="blocked", error=str(e)
                )
            except FetchError as e:
                error = str(e)
                log.warning("bing: fetch failed for query %r: %s", q, e)
                continue

        return hits, SourceStatus(
            name=self.name,
            method=self.method,
            status="failed" if (error and not hits) else "ok",
            results=len(hits),
            error=error,
        )

    def _parse(
        self,
        html: str,
        query: str,
        brand_tokens: set[str],
        skip_domains: set[str],
        seen: set[str],
    ) -> list[Hit]:
        tree = HTMLParser(html)
        hits: list[Hit] = []

        for result in tree.css("li.b_algo"):
            h2 = result.css_first("h2 a")
            if not h2:
                continue
            href = h2.attributes.get("href", "")
            cite = result.css_first("cite")
            cite_text = cite.text(strip=True) if cite else ""

            real_url = _extract_url(href, cite_text)
            if not real_url or not real_url.startswith("http"):
                continue

            domain = urlparse(real_url).hostname or ""
            # Strip www. for comparison
            bare_domain = domain.removeprefix("www.")
            if bare_domain in skip_domains or domain in skip_domains:
                continue

            uid = hashlib.sha256(real_url.encode()).hexdigest()[:32]
            if uid in seen:
                continue
            seen.add(uid)

            title = h2.text(strip=True) or None

            # Snippet: prefer the structured lineclamp paragraph, fall back to caption
            desc_node = result.css_first(".b_lineclamp1, .b_lineclamp2, .b_caption p, .b_paractl")
            snippet: str | None = None
            if desc_node:
                raw = desc_node.text(strip=True)
                # Strip trailing "Read more" injected by Bing
                if raw.endswith("Read more"):
                    raw = raw[:-9].strip().rstrip("…").strip()
                # Strip leading date prefix ("3 days ago · ...")
                if "·" in raw:
                    pre, rest = raw.split("·", 1)
                    if len(pre.strip()) <= 20:
                        raw = rest.strip()
                snippet = raw or None

            # Publisher: Bing puts structured facts (Developer, Version, OS) in b_factrow
            publisher: str | None = None
            for fact_div in result.css(".b_factrow li div, .b_factrow .b_sritem"):
                text = fact_div.text(strip=True)
                if text.lower().startswith("developer:"):
                    publisher = text.split(":", 1)[1].strip() or None
                    break

            # Icon URL: stored in the mediaurl query param of the image explore link
            icon_url: str | None = None
            img_link = result.css_first(".b_imgcap_img a")
            if img_link:
                img_href = img_link.attributes.get("href", "")
                params = parse_qs(urlparse(img_href).query)
                mediaurl = params.get("mediaurl", [None])[0]
                if mediaurl and mediaurl.startswith("http"):
                    icon_url = mediaurl

            hits.append(
                Hit(
                    id=uid,
                    type=HitType.web_page,
                    title=title,
                    snippet=snippet,
                    publisher=publisher,
                    icon_url=icon_url,
                    url=real_url,
                    host=self._make_host(real_url),
                    found_via=[
                        FoundVia(source=self.name, method=self.method, query=query)
                    ],
                    relevance_score=0,
                    discovered_at=datetime.datetime.now(datetime.UTC),
                )
            )

        return hits
