# app/utils/url_tools.py
"""URL normalization and deduplication utilities."""

from __future__ import annotations

import hashlib
import logging
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import tldextract


log = logging.getLogger(__name__)

# Tracking parameter prefixes to strip from URLs before deduplication
_TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "fbclid", "gclid", "msclkid", "ref", "referrer",
        "source", "mc_eid", "yclid", "_ga",
    }
)

# Direct file download extensions — listing URLs are fine, these are not stored
_DOWNLOAD_EXTENSIONS = frozenset(
    {".apk", ".xapk", ".apks", ".obb", ".zip", ".rar", ".tar", ".gz",
     ".7z", ".exe", ".dmg", ".pkg", ".deb", ".rpm"}
)


def normalize_url(url: str) -> str:
    """
    Canonical form of a URL for deduplication.

    - lowercases scheme and host
    - removes trailing slash from path (except root)
    - strips tracking params
    - sorts remaining query params
    - drops fragment
    """
    try:
        p = urlparse(url.strip())
        scheme = p.scheme.lower()
        netloc = p.netloc.lower()
        path = p.path.rstrip("/") or "/"
        params_raw = parse_qs(p.query, keep_blank_values=False)
        cleaned = {k: v for k, v in params_raw.items() if k.lower() not in _TRACKING_PARAMS}
        query = urlencode(sorted(cleaned.items()), doseq=True)
        return urlunparse((scheme, netloc, path, "", query, ""))
    except Exception as exc:
        log.warning("could not normalize URL %r: %s: %s", url, type(exc).__name__, exc)
        return url


def url_id(url: str) -> str:
    """Stable SHA-256 hex ID for a normalized URL."""
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()[:32]


def registrable_domain(url_or_domain: str) -> str:
    """Return the registrable domain (e.g. 'example.co.uk') from a URL or domain."""
    ext = tldextract.extract(url_or_domain)
    if ext.domain and ext.suffix:
        return f"{ext.domain}.{ext.suffix}"
    return ext.domain or url_or_domain


def is_download_url(url: str) -> bool:
    """True if the URL looks like a direct file download link."""
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in _DOWNLOAD_EXTENSIONS)


def decode_idn(domain: str) -> str:
    """Decode a punycode IDN domain to Unicode for similarity comparison."""
    try:
        parts = domain.split(".")
        decoded = ".".join(
            p.encode("ascii").decode("idna") if p.startswith("xn--") else p
            for p in parts
        )
        return decoded
    except (UnicodeError, UnicodeDecodeError):
        return domain