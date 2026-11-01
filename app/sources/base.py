# app/sources/base.py
"""Base interface for all source adapters."""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Literal

from app.api.models import FoundVia, Hit, HitType, HostInfo, ScanContext, SourceMethod, SourceStatus
from app.services.keyword_expansion import fold_homoglyphs

log = logging.getLogger(__name__)


def scan_countries(ctx: ScanContext) -> list[str]:
    """The storefronts every scan searches (DEFAULT_COUNTRIES in settings). The request does not choose them."""
    from app.config.settings import get_settings
    return get_settings().default_country_list()


# Generic package-name segments that are meaningless as search queries
_GENERIC_PKG_TOKENS = frozenset({
    "app", "apps", "com", "net", "org", "io", "in", "co", "mobile",
    "android", "apk", "main", "pro", "lite", "free", "beta", "dev",
    "debug", "release", "store", "market", "play",
})


def pkg_queries(ctx: ScanContext) -> list[str]:
    """Return [brand_name] plus the meaningful last segment of android_package (if distinct).

    Filters out generic tokens like 'app', 'com', 'mobile' that produce massive
    false-positive noise when used as bare search queries.
    """
    brand = ctx.request.brand_name
    queries: list[str] = [brand]
    pkg = ctx.request.android_package or ""
    if pkg:
        last = pkg.split(".")[-1].lower()
        brand_lower = {t.lower() for t in brand.split()}
        if last not in _GENERIC_PKG_TOKENS and last not in brand_lower and len(last) >= 4:
            queries.append(last)
    return queries


# ── Reading rendered search pages honestly ─────────────────────────────────────
# A scraper that finds no result cards must say *why*: a block page, a dead endpoint and a changed layout all
# look like "0 results" unless the adapter checks. These helpers give the Playwright/HTTP scrapers one shared
# vocabulary so a blocked source is reported as blocked instead of as an empty `ok`.

_BLOCK_TEXT_RE = re.compile(
    r"captcha|verify you are human|security verification|just a moment|attention required|"
    r"access denied|unusual traffic|sorry! something went wrong",
    re.IGNORECASE,
)
_EMPTY_TEXT_RE = re.compile(
    r"\bno (?:results?|apps?|matches|games?)\b|\b0 results?\b|nothing found|did not match|didn'?t match",
    re.IGNORECASE,
)
_NETWORK_BLOCK_RE = re.compile(
    r"ERR_TIMED_OUT|ERR_CONNECTION_(?:RESET|REFUSED|TIMED_OUT|CLOSED)|ERR_NAME_NOT_RESOLVED|"
    r"ERR_SSL|ERR_TUNNEL|handshake|Timeout \d+ms exceeded",
    re.IGNORECASE,
)


def page_problem(status: int | None, title: str = "", text: str = "") -> str | None:
    """Why a loaded page cannot be read as "no results" (a block or error page); None when it looks fine."""
    if status is not None and status >= 400:
        return f"HTTP {status}"
    if _BLOCK_TEXT_RE.search(f"{title} {text[:600]}"):
        return "block / challenge page"
    return None


def says_no_results(text: str) -> bool:
    """Does the page itself state that nothing matched? (A genuine empty result, as opposed to a broken parse.)"""
    return bool(_EMPTY_TEXT_RE.search(text[:4000]))


def is_network_block(error: str) -> bool:
    """A navigation error meaning the site is unreachable or refusing this network, not a bug in the adapter."""
    return bool(_NETWORK_BLOCK_RE.search(error))


def scrape_status(
    name: str, method: SourceMethod, hits: list[Hit], *, errors: list[str], blocked: list[str],
) -> SourceStatus:
    """Final status of a scraper from what went wrong along the way.

    Nothing found + blocked/unreachable  -> blocked   (the empty result is not trustworthy)
    Nothing found + other errors         -> failed
    Some found + any problem             -> partial
    """
    problems = [*blocked, *errors]
    if not problems:
        status: Literal["ok", "partial", "failed", "blocked"] = "ok"
    elif hits:
        status = "partial"
    elif blocked:
        status = "blocked"
    else:
        status = "failed"
    return SourceStatus(
        name=name, method=method, status=status, results=len(hits),
        error="; ".join(dict.fromkeys(problems))[:300] or None,
    )


# A package name embedded in a store's URL or page source, e.g. "com.example.app". Domain names are skipped.
_EMBEDDED_PKG_RE = re.compile(r"\b((?:com|net|org|in|io|co)\.[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]+)+)\b")
_DOMAIN_TLDS = (".com", ".net", ".org", ".io", ".co", ".in")


def embedded_package(text: str) -> str:
    """The first package name in `text` that is not a domain name, or "" when there is none."""
    for m in _EMBEDDED_PKG_RE.finditer(text):
        if not m.group(1).endswith(_DOMAIN_TLDS):
            return m.group(1)
    return ""


def brand_words(brand: str) -> set[str]:
    """The lower-cased words of a brand name that a result must contain to count (short words are ignored)."""
    return {t.lower() for t in fold_homoglyphs(brand).split() if len(t) >= 3}


def scraped_hit(
    adapter: SourceAdapter, query: str, *, url: str, host_name: str, host_domain: str,
    group: str | None = None, hit_type: HitType = HitType.app_listing, hit_id: str | None = None, **fields,
) -> Hit:
    """A hit read from a scraped page. Its id is the hash of its URL, and the adapter's name, method and host group
    are filled in. Whatever the page gives (app name, package, publisher, icon...) is passed in as `fields`."""
    return Hit(
        id=hit_id or hashlib.sha256(url.encode()).hexdigest()[:32],
        type=hit_type,
        url=url,
        host=HostInfo(group=group or adapter.host_group, name=host_name, domain=host_domain, is_official_store=False),
        found_via=[FoundVia(source=adapter.name, method=adapter.method, query=query)],
        relevance_score=0,
        discovered_at=datetime.datetime.now(datetime.timezone.utc),
        **fields,
    )


def country_label(term: str, country: str, countries: list[str]) -> str:
    """Query label of a hit: with several countries the country is part of it, so the per-query result cap
    applies per country instead of squeezing every country into one budget."""
    return f"{term} [{country}]" if len(countries) > 1 else term


class SourceAdapter(ABC):
    name: str
    method: SourceMethod
    host_group: str  # e.g. "official_store", "apk_site", "file_host", "code_host"

    # Max hits a source may contribute *per distinct query*. Sources return
    # relevance-ordered results, so we keep the first N of each query — e.g. a
    # "petpooja" and a "petpooja apk" search each keep up to max_results, so
    # one query never starves another. Rogue-heavy sources (APK sites, code
    # hosts, web search, threat intel) keep the default 20; official stores
    # override this down to 10 (expect 1 official + a few variants).
    max_results: int = 20

    @abstractmethod
    async def search(
        self, ctx: ScanContext
    ) -> tuple[list[Hit], SourceStatus]:
        """
        Execute the search and return (hits, status).

        Must never raise. All exceptions are caught and returned as a
        SourceStatus with status="failed".
        """

    async def _run(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        """Wrapper that times execution, caps results, and catches all exceptions."""
        from app.config.settings import get_settings

        start = time.monotonic()
        limit = get_settings().source_timeout_seconds
        try:
            try:
                hits, status = await asyncio.wait_for(self.search(ctx), limit)
            except TimeoutError:
                log.warning("%s: stopped after %ss", self.name, limit)
                return [], SourceStatus(
                    name=self.name, method=self.method, status="failed", results=0,
                    error=f"timed out after {limit}s", duration_ms=int((time.monotonic() - start) * 1000),
                )
            # Enforce the result cap centrally, per distinct query — keep the
            # first N hits of each query (identified by found_via[0].query).
            raw = len(hits)
            capped: list[Hit] = []
            per_query: dict[str, int] = {}
            for h in hits:
                q = h.found_via[0].query if h.found_via else ""
                n = per_query.get(q, 0)
                if n < self.max_results:
                    capped.append(h)
                    per_query[q] = n + 1
            if len(capped) < raw:
                status.raw_results = raw
            hits = capped
            status.results = len(hits)
            status.duration_ms = int((time.monotonic() - start) * 1000)
            return hits, status
        except Exception as exc:
            duration = int((time.monotonic() - start) * 1000)
            # Full traceback at exception level so an adapter crash is never silent.
            log.exception("%s adapter crashed: %s", self.name, exc)
            return [], SourceStatus(
                name=self.name,
                method=self.method,
                status="failed",
                results=0,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=duration,
            )
