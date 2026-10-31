# app/sources/lookalike_domain_adapter.py
"""Lookalike-domain / fake app-store website discovery.

Rogue apps are often pushed from a site that imitates the brand's own domain
(petpooja-apk.com, petpooja.app). This adapter permutes official_domain
(typos, homoglyphs, TLD swaps, brand+keyword), resolves each candidate in DNS
and records the ones that exist, with the page title when the homepage is
reachable. Existence alone is a signal; scoring weighs the domain similarity.

Skipped when no official_domain is supplied. Only DNS lookups and one
SSRF-guarded GET per resolving domain are made; nothing is downloaded.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import re
import socket

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from app.api.models import (
    EvidenceRecord,
    FoundVia,
    Hit,
    HitType,
    HostInfo,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.services.keyword_expansion import _homoglyph_variants, _typo_variants
from app.services.page_assessment import assess_page, extract_apk_links, find_download_pages
from app.sources.base import SourceAdapter
from app.utils.http_client import BlockedError, FetchError, decode_response, fetch
from app.utils.ssrf_guard import SSRFError, check_url
from app.utils.url_tools import registrable_domain

log = logging.getLogger(__name__)

_ALT_TLDS = [
    # Generic
    "com", "net", "org", "co", "io", "app", "xyz", "online", "site", "top", "info", "club",
    # India
    "in", "co.in", "net.in",
    # United States
    "us",
    # United Kingdom
    "uk", "co.uk", "org.uk", "me.uk",
    # Australia
    "com.au", "net.au", "org.au",
    # South Africa
    "co.za", "org.za",
    # Saudi Arabia
    "com.sa", "net.sa", "sa",
    # Mexico
    "com.mx", "net.mx", "mx",
    # Malaysia
    "com.my", "net.my", "my",
    # Montenegro (ME is also widely used as a vanity TLD)
    "me",
]
_KEYWORDS = ["apk", "app", "download", "mod", "official", "pro", "free", "online", "login", "get"]
_MAX_CANDIDATES = 300
_MAX_PAGE_FETCHES = 30
_DNS_TIMEOUT = 4.0
_DNS_CONCURRENCY = 20
# Corporate brand-protection registrars: a candidate parked here is almost always the
# brand's own defensive registration. Heuristic — extend as needed.
_DEFENSIVE_FINAL_DOMAINS = frozenset({
    "gcd.com", "markmonitor.com", "cscglobal.com", "cscdbs.com", "corporatedomains.com",
    "safenames.net", "com-laude.com", "brandsight.com",
})
_DEFENSIVE_TITLES = ("protected domain holder", "corporate domains")
_LABEL_RE = re.compile(r"^[a-z0-9-]{1,63}$")


def generate_candidates(official_domain: str, limit: int = _MAX_CANDIDATES) -> list[str]:
    """Permute the official registrable domain. Never includes the original."""
    reg = registrable_domain(official_domain).lower()
    if "." not in reg:
        return []
    label, suffix = reg.split(".", 1)

    labels: set[str] = set()
    labels.update(_typo_variants(label, max_variants=40))
    labels.update(_homoglyph_variants(label, max_variants=15))
    for kw in _KEYWORDS:
        labels.update({f"{label}-{kw}", f"{label}{kw}", f"{kw}-{label}", f"{kw}{label}"})
    labels.add(f"{label}-")

    out: list[str] = []

    def add(domain: str) -> None:
        try:
            ascii_domain = domain.encode("idna").decode("ascii")
        except UnicodeError:
            return
        lbl = ascii_domain.split(".", 1)[0]
        if ascii_domain != reg and _LABEL_RE.match(lbl) and not lbl.startswith("-") \
                and not lbl.endswith("-") and ascii_domain not in out:
            out.append(ascii_domain)

    # TLD swaps first: highest-precision permutation
    for tld in _ALT_TLDS:
        if tld != suffix:
            add(f"{label}.{tld}")
    for lbl in sorted(labels, key=lambda x: (abs(len(x) - len(label)), x)):
        add(f"{lbl}.{suffix}")
    return out[:limit]


async def _resolves(domain: str, sem: asyncio.Semaphore) -> bool:
    async with sem:
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(loop.run_in_executor(None, socket.getaddrinfo, domain, None), _DNS_TIMEOUT)
        except socket.gaierror:
            return False  # NXDOMAIN / no such host: the normal case for a permutation, not an error
        except (asyncio.TimeoutError, OSError) as exc:
            log.info("lookalike: DNS lookup of %s failed: %s: %s", domain, type(exc).__name__, exc)
            return False
    try:
        check_url(f"https://{domain}")  # drops domains resolving to private/loopback space
    except SSRFError as exc:
        log.info("lookalike: dropping %s: %s", domain, exc)
        return False
    return True


async def _safe_apk_link(html: str, page_url: str) -> str | None:
    """First APK link on the page that passes the SSRF guard; the page is untrusted, so each link is re-checked."""
    for link in extract_apk_links(html, page_url):
        try:
            await asyncio.to_thread(check_url, link)
        except SSRFError as exc:
            log.info("lookalike: dropping apk link %s: %s", link, exc)
            continue
        return link
    return None


async def _find_apk(html: str, page_url: str) -> str | None:
    """The APK a fake-app page offers: a link on the page itself, else one hop through its Download button.

    Fetching is limited to pages on the same site (find_download_pages) and goes through fetch(), which has the
    SSRF guard and refuses direct file downloads; the file itself is only ever fetched by the APK analyzer.
    """
    apk = await _safe_apk_link(html, page_url)
    if apk:
        return apk
    for page in find_download_pages(html, page_url):
        try:
            resp = await fetch(page, rotate_ua=True, retries=0, timeout=8)
        except Exception as exc:  # down, blocked, or not a page: try the next one
            log.info("lookalike: download page %s failed: %s: %s", page, type(exc).__name__, exc)
            continue
        apk = await _safe_apk_link(decode_response(resp), str(resp.request.url))
        if apk:
            return apk
    return None


async def apk_for_page(url: str, brand: str, official_domain: str | None) -> str | None:
    """The APK a page offers, if it looks like a fake-app page (brand bait). None for any other page or failure.

    Used for pages other sources found (search results, lookalike domains); same SSRF-guarded fetch and
    one-hop Download-button follow as the lookalike probe.
    """
    try:
        resp = await fetch(url, rotate_ua=True, retries=0, timeout=8)
        html = decode_response(resp)
        if assess_page(html, brand, official_domain) in ("brand_bait", "mentions_brand"):
            return await _find_apk(html, str(resp.request.url))
    except Exception as exc:  # down, blocked or not HTML: no APK to report
        log.info("apk discovery: %s failed: %s: %s", url, type(exc).__name__, exc)
    return None


def _probe_priority(domain: str, label: str) -> int:
    """Probe order, lowest first. Keyword permutations of the brand (whatsapp-apk.com) are the scam pattern, so
    they go first; typo and look-alike spellings come next; plain TLD swaps (mostly parked) go last."""
    name = domain.split(".", 1)[0]
    remainder = name.replace(label, "", 1).strip("-")
    if label in name and any(kw in remainder for kw in _KEYWORDS):
        return 0
    return 1 if name != label else 2


async def _fetch_home(domain: str):
    """The homepage over HTTPS, or over plain HTTP when HTTPS fails at the transport level (broken or missing
    certificate). A refusal (BlockedError) is final: asking again over HTTP only repeats it."""
    try:
        return await fetch(f"https://{domain}", rotate_ua=True, retries=0, timeout=8)
    except BlockedError:
        raise
    except FetchError as https_error:
        try:
            return await fetch(f"http://{domain}", rotate_ua=True, retries=0, timeout=8)
        except FetchError:
            raise https_error


def _unreachable_reason(exc: BaseException) -> str:
    """Why a probe got no page, in words a triage or takedown reader can act on."""
    if isinstance(exc, BlockedError):
        return "blocked (bot protection, 403 or rate limit)"
    chain, e = [], exc
    while e is not None:
        chain.append(f"{type(e).__name__} {e}")
        e = e.__cause__ or e.__context__
    text = " ".join(chain)
    if "SSL" in text or "TLS" in text:
        return "TLS error (no valid certificate for this name)"
    if "Timeout" in text:
        return "timed out"
    if "ConnectError" in text:
        return "connection failed"
    if "Server error '5" in text:
        return "server error (5xx)"
    if "Client error '4" in text:
        return "client error (4xx)"
    return "no response"


async def _probe(
    domain: str, brand: str, official_domain: str,
) -> tuple[str | None, str | None, str | None, EvidenceRecord | None]:
    """Fetch the homepage once. Returns (title, registrable domain after redirects, APK link if the page is
    brand bait, the page check with the page's status and hash).

    The page check is kept on the hit, so parked / blank / brand bait ranks the domain whether or not
    evidence files are saved.
    """
    try:
        resp = await _fetch_home(domain)
        html = decode_response(resp)
        node = HTMLParser(html).css_first("title")
        title = node.text(strip=True) if node else ""
        page_url = str(resp.request.url)
        kind = assess_page(html, brand, official_domain)
        apk_url = await _find_apk(html, page_url) if kind in ("brand_bait", "mentions_brand") else None
        check = EvidenceRecord(
            captured_at=datetime.datetime.now(datetime.timezone.utc),
            http_status=resp.status_code,
            final_url=page_url,
            page_sha256=hashlib.sha256(resp.content).hexdigest(),
            assessment=kind,
        )
        return (title[:150] or None), registrable_domain(page_url).lower(), apk_url, check
    except Exception as exc:  # a candidate that is down, blocks us or is not a page is expected
        reason = _unreachable_reason(exc)
        log.info("lookalike: probe of %s: %s", domain, reason)
        return None, None, None, EvidenceRecord(
            captured_at=datetime.datetime.now(datetime.timezone.utc), error=f"unreachable: {reason}",
        )


class LookalikeDomainAdapter(SourceAdapter):
    name = "lookalike_domains"
    method = SourceMethod.scrape
    host_group = "lookalike_domain"
    max_results = 40

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        official = (ctx.request.official_domain or "").strip().lower()
        if not official:
            return [], SourceStatus(
                name=self.name, method=self.method, status="skipped",
                error="official_domain not set",
            )

        candidates = generate_candidates(official)
        sem = asyncio.Semaphore(_DNS_CONCURRENCY)
        results = await asyncio.gather(*[_resolves(d, sem) for d in candidates])
        live = [d for d, ok in zip(candidates, results) if ok]

        # The probe limit goes to the likeliest lookalikes first, not to whichever came first in generation order
        label = registrable_domain(official).split(".", 1)[0]
        to_probe = sorted(live, key=lambda d: _probe_priority(d, label))[:_MAX_PAGE_FETCHES]
        probes = await asyncio.gather(*[_probe(d, ctx.request.brand_name, official) for d in to_probe])
        probe_by_domain = dict(zip(to_probe, probes))

        # A candidate that redirects to the brand's own domain is a defensive
        # registration, not a lookalike.
        official_regs = {
            registrable_domain(d).lower()
            for d in [official, *ctx.request.official_domains] if d
        }

        now = datetime.datetime.now(datetime.timezone.utc)
        hits: list[Hit] = []
        for domain in live:
            title, final_domain, apk_url, check = probe_by_domain.get(domain, (None, None, None, None))
            if final_domain in official_regs or final_domain in _DEFENSIVE_FINAL_DOMAINS:
                continue
            if title and any(t in title.lower() for t in _DEFENSIVE_TITLES):
                continue
            # Only a domain that offers an APK is reported: a bare domain that resolves is not a mobile app finding
            if not apk_url:
                continue
            url = f"https://{domain}/"
            hits.append(Hit(
                id=hashlib.sha256(url.encode()).hexdigest()[:32],
                type=HitType.web_page,
                app_name=domain.split(".", 1)[0],
                title=title or f"Lookalike domain {domain} (resolves in DNS)",
                url=url,
                apk_url=apk_url,
                evidence=check,
                host=HostInfo(group=self.host_group, name="Lookalike domain", domain=domain),
                found_via=[FoundVia(source=self.name, method=self.method, query=official)],
                relevance_score=0,
                discovered_at=now,
            ))

        return hits, SourceStatus(
            name=self.name, method=self.method, status="ok", results=len(hits),
        )

