"""APKMirror search adapter — RSS feed.

APKMirror's HTML search is behind Cloudflare Bot Fight Mode (HTTP 403 from
server IPs). The RSS feed endpoint bypasses the challenge and returns the
same search results as structured XML.

RSS endpoint: https://www.apkmirror.com/feed/?searchtype=apk&s={query}
App pages:    https://www.apkmirror.com/apk/{dev-slug}/{app-slug}/

APKMirror is valuable for rogue detection because:
  - It tracks every APK version with signing certificate metadata
  - Rogue/repackaged APKs often appear here under different signers
  - The app-slug in the URL approximates the package name
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

from app.api.models import (
    ApkAnalysisResult,
    FoundVia,
    Hit,
    HitType,
    HostInfo,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.services.keyword_expansion import fold_homoglyphs
from app.sources.base import SourceAdapter, page_problem, pkg_queries, scrape_status
from app.utils.http_client import BlockedError, FetchError, decode_response, fetch

log = logging.getLogger(__name__)

_BASE = "https://www.apkmirror.com"
_RSS = _BASE + "/feed/?searchtype=apk&s={q}"

# /apk/{dev-slug}/{app-slug}/ — app-slug approximates the package name
_PKG_RE = re.compile(r"/apk/[^/]+/([^/]+)/")
# SHA1 fingerprint on release pages
_SHA1_RE = re.compile(r'\b([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){19}|[0-9a-fA-F]{40})\b')

_RSS_HEADERS = {
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


async def _fetch_cert_sha1(app_url: str) -> str | None:
    """Visit the app overview page, follow the first release link, return SHA1 cert."""
    from selectolax.lexbor import LexborHTMLParser as HTMLParser
    try:
        resp = await fetch(app_url, rotate_ua=True, retries=1)
        tree = HTMLParser(decode_response(resp))
        release_href: str | None = None
        for a in tree.css(".listWidget .appRow .appRowTitle a, .widget_appmanager_recentpostswidget a"):
            href = a.attributes.get("href", "")
            if href and "release" in href:
                release_href = href if href.startswith("http") else f"{_BASE}{href}"
                break
        if not release_href:
            return None
        resp2 = await fetch(release_href, rotate_ua=True, retries=1)
        m = _SHA1_RE.search(decode_response(resp2))
        if m:
            return m.group(1).upper().replace(":", "")
    except Exception as e:
        log.debug("apkmirror: cert fetch failed for %s: %s", app_url, e)
    return None


class APKMirrorAdapter(SourceAdapter):
    name = "apkmirror"
    method = SourceMethod.scrape
    host_group = "apk_site"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        brand = ctx.request.brand_name
        brand_tokens = {t.lower() for t in fold_homoglyphs(brand).split() if len(t) >= 3}
        official_cert = ""

        queries = pkg_queries(ctx)

        hits: list[Hit] = []
        seen: set[str] = set()
        errors: list[str] = []
        blocked: list[str] = []

        for q in queries[:2]:
            url = _RSS.format(q=quote_plus(q))
            try:
                resp = await fetch(url, rotate_ua=True, headers=_RSS_HEADERS)
            except BlockedError as e:
                blocked.append(str(e))
                break  # Cloudflare blocks the whole client, the second query will not differ
            except FetchError as e:
                errors.append(str(e))
                continue

            body = decode_response(resp)
            try:
                hits.extend(self._parse_rss(body, q, brand_tokens, seen))
            except ET.ParseError as e:
                # A 200 that is not XML is the Cloudflare challenge page, not an empty result set
                problem = page_problem(resp.status_code, "", body)
                (blocked if problem else errors).append(
                    f"{problem or 'unparseable RSS reply'} from {_BASE}/feed/ ({e})"
                )

        if official_cert and hits:
            enriched = await asyncio.gather(
                *[self._enrich_cert(h, official_cert) for h in hits[:3]],
                return_exceptions=True,
            )
            hits[:3] = [h for h in enriched if isinstance(h, Hit)]

        return hits, scrape_status(self.name, self.method, hits, errors=errors, blocked=blocked)

    async def _enrich_cert(self, hit: Hit, official_cert: str) -> Hit:
        sha1 = await _fetch_cert_sha1(hit.url)
        if not sha1:
            return hit
        cert_match = sha1.lower() == official_cert.lower() if official_cert else None
        return hit.model_copy(update={
            "apk_analysis": ApkAnalysisResult(cert_sha256=sha1, cert_match=cert_match)
        })

    def _parse_rss(
        self, xml_text: str, query: str, brand_tokens: set[str], seen: set[str]
    ) -> list[Hit]:
        hits: list[Hit] = []
        root = ET.fromstring(xml_text)  # ParseError propagates: the caller tells a challenge page from an empty feed

        for item in root.findall(".//item"):
            title_el = item.find("title")
            link_el = item.find("link")
            if title_el is None or link_el is None:
                continue

            name = (title_el.text or "").strip()
            # Strip version/type suffixes: "PhonePe 24.04.1 APK" → "PhonePe"
            name_clean = re.sub(r'\s+\d[\d.]+.*$', '', name).strip() or name

            link = (link_el.text or "").strip()
            if not name or not link or "apkmirror.com" not in link:
                continue

            name_lower = fold_homoglyphs(name).lower()
            if brand_tokens and not any(tok in name_lower for tok in brand_tokens):
                continue

            uid = hashlib.sha256(link.encode()).hexdigest()[:32]
            if uid in seen:
                continue
            seen.add(uid)

            m = _PKG_RE.search(link)
            pkg_slug = m.group(1).replace("-", ".") if m else None

            # Publisher from <dc:creator> or description
            creator_el = item.find("{http://purl.org/dc/elements/1.1/}creator")
            publisher = (creator_el.text or "").strip() or None

            hits.append(Hit(
                id=uid,
                type=HitType.app_listing,
                app_name=name_clean,
                package_id=pkg_slug,
                publisher=publisher,
                url=link,
                host=HostInfo(
                    group=self.host_group,
                    name="APKMirror",
                    domain="apkmirror.com",
                    is_official_store=False,
                ),
                found_via=[FoundVia(source=self.name, method=self.method, query=query)],
                relevance_score=0,
                discovered_at=datetime.datetime.now(datetime.timezone.utc),
            ))

        return hits
