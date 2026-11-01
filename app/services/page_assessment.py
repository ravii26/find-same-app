# app/services/page_assessment.py
"""Classify a captured web page so lookalike domains can be ranked by what they actually serve.

A registered lookalike domain is only a candidate. What separates a threat from a parked name is
the page, so we read the HTML we already saved as evidence:

  parked          domain-for-sale / parking / default server page
  blank           (almost) no visible text AND no brand reference in the page code — empty / "Loading..."
  brand_bait      the classic phish/fake-app page, in either of two shapes:
                    * visible text repeats the brand with lure wording (bonus, reward, claim, download ...)
                    * the page code fires a payment/app deep link (phonepe://, upi://pay, intent://)
                      for the brand behind reward bait — scam kits are JS-driven, so there may be almost
                      no visible text. Both require that the page never links to the brand's real site.
  mentions_brand  names the brand but is not clearly bait
  neutral         anything else

Heuristics, not verdicts: they only re-rank hits, and every outcome is recorded with the evidence.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from selectolax.lexbor import LexborHTMLParser

from app.services.brand_match import compact, count_mentions
from app.utils.url_tools import is_download_url, registrable_domain

PARKED = re.compile(
    r"(domain (name )?(is )?(for sale|available)|buy (this|the) domain|this domain (may be|is) for sale|"
    r"parked (free|domain|by)|domain parking|sedo\.com|dan\.com|afternic|hugedomains|"
    r"default web ?page|welcome to nginx|apache2 (ubuntu )?default|it works!|under construction|coming soon|"
    r"site not found|account suspended|domain (has )?expired)",
    re.IGNORECASE,
)
LURE = re.compile(
    r"\b(bonus|rewards?|cashback|claim|winner|prize|gift|kyc|verify|verification|login|sign ?in|download|"
    r"install|apk|free|offer|refund|participat\w*|limited|expires?|urgent|congratulations|scratch|spin)\b",
    re.IGNORECASE,
)
_BLANK_TEXT = 40       # fewer visible characters than this = blank (unless the code references the brand)
_BAIT_MENTIONS = 2

# app/payment deep-link schemes: any custom scheme plus upi://, minus ordinary web/browser ones
_SCHEME = re.compile(r"\b([a-z][a-z0-9+.\-]{2,24}):(?://|\?)", re.IGNORECASE)
_ORDINARY_SCHEMES = frozenset({
    "http", "https", "ws", "wss", "ftp", "data", "blob", "file", "about", "javascript", "mailto", "tel", "sms",
    "chrome", "chrome-extension", "moz-extension", "resource", "view-source", "webcal", "geo", "market",
})


def _visible_text(html: str) -> str:
    tree = LexborHTMLParser(html)
    for node in tree.css("script, style, noscript, template"):
        node.decompose()
    text = tree.body.text(separator=" ") if tree.body else tree.text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()


def _links_to(html: str, official_domain: str | None) -> bool:
    if not official_domain:
        return False
    official = registrable_domain(official_domain).lower()
    for node in LexborHTMLParser(html).css("a[href], form[action]"):
        target = node.attributes.get("href") or node.attributes.get("action") or ""
        host = urlparse(target).hostname
        if host and registrable_domain(host).lower() == official:
            return True
    return False


def _brand_deeplink(html: str, brand: str) -> bool:
    """Does the page code launch an app/payment deep link tied to the brand (or UPI generally)?"""
    brand_c = compact(brand)
    for m in _SCHEME.finditer(html):
        scheme = m.group(1).lower()
        if scheme in _ORDINARY_SCHEMES:
            continue
        if scheme == "upi" or (len(brand_c) >= 4 and brand_c in compact(scheme)):
            return True
    return False


_APK_EXTENSIONS = (".apk", ".xapk", ".apks")

# Scam kits often start the download from script (location.href = "app.apk"), with no <a> to find
_SCRIPT_APK = re.compile(r"""["'(]([^"'()\s<>]+\.(?:apk|xapk|apks)(?:\?[^"'()\s<>]*)?)["')]""", re.IGNORECASE)
_DOWNLOADISH = re.compile(r"download|install|get[-_ ]?app|apk|android|free[-_ ]?app", re.IGNORECASE)


def _accept_apk(url: str, base_url: str) -> str | None:
    absolute = urljoin(base_url, url.strip())
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return absolute if parsed.path.lower().endswith(_APK_EXTENSIONS) else None


def extract_apk_links(html: bytes | str, base_url: str, limit: int = 3) -> list[str]:
    """Absolute http(s) links to .apk/.xapk/.apks files that a page offers (nothing is fetched).

    Looks at <a href> links first, then at quoted file names inside the page's scripts. Candidates only:
    the caller must run each through the SSRF guard before downloading it.
    """
    raw = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    candidates = [(node.attributes.get("href") or "") for node in LexborHTMLParser(raw).css("a[href]")]
    candidates += _SCRIPT_APK.findall(raw)
    found: list[str] = []
    for candidate in candidates:
        url = _accept_apk(candidate, base_url) if candidate.strip() else None
        if url and url not in found:
            found.append(url)
        if len(found) >= limit:
            break
    return found


def find_download_pages(html: bytes | str, base_url: str, limit: int = 2) -> list[str]:
    """Same-site pages that a "Download" / "Install" / "Get the app" button leads to (nothing is fetched).

    These are the pages a fake-app site hides the file behind. Direct file links are not returned (see
    extract_apk_links), and neither are other sites: the follow-up is one hop on the same registrable domain.
    """
    raw = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    here = registrable_domain(base_url).lower()
    found: list[str] = []
    for node in LexborHTMLParser(raw).css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(base_url, href)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        if registrable_domain(url).lower() != here or is_download_url(url):
            continue
        if not _DOWNLOADISH.search(f"{node.text(strip=True)} {parsed.path}"):
            continue
        clean = url.split("#", 1)[0]
        if clean != base_url.split("#", 1)[0] and clean not in found:
            found.append(clean)
        if len(found) >= limit:
            break
    return found


def assess_page(html: bytes | str, brand: str, official_domain: str | None = None) -> str:
    """Return one of: parked, blank, brand_bait, mentions_brand, neutral."""
    raw = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    text = _visible_text(raw)
    if PARKED.search(text) or PARKED.search(raw[:4000]):
        return "parked"

    visible_mentions = count_mentions(text, brand)
    code_mentions = count_mentions(raw, brand)          # includes scripts, attributes, deep links
    links_official = _links_to(raw, official_domain)
    lure = bool(LURE.search(text) or LURE.search(raw))

    if not links_official and lure:
        if _brand_deeplink(raw, brand) and code_mentions >= 1:
            return "brand_bait"
        if visible_mentions >= _BAIT_MENTIONS and LURE.search(text):
            return "brand_bait"
    if len(text) < _BLANK_TEXT and code_mentions < _BAIT_MENTIONS:
        return "blank"
    return "mentions_brand" if max(visible_mentions, code_mentions) else "neutral"
