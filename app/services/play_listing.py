# app/services/play_listing.py
"""Google Play listing facts used by scoring: who publishes an app, and which permissions its listing shows.

Both come from google_play_scraper, so no APK is downloaded. The blocking calls run in a worker thread through
call_with_backoff, which shares the request limit and backoff with the rest of the scanner.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from urllib.parse import urlparse

from app.utils.http_client import call_with_backoff
from app.utils.url_tools import registrable_domain

log = logging.getLogger(__name__)

# Manifest permission constants that are high-risk for a lookalike to request beyond the official app.
# Short names, no android.permission. prefix.
ANDROID_DANGEROUS: frozenset[str] = frozenset({
    "READ_SMS", "RECEIVE_SMS", "SEND_SMS", "READ_CONTACTS", "READ_CALL_LOG",
    "RECORD_AUDIO", "CAMERA", "BIND_ACCESSIBILITY_SERVICE", "REQUEST_INSTALL_PACKAGES",
    "USE_BIOMETRIC", "USE_FINGERPRINT", "ACCESS_FINE_LOCATION", "SYSTEM_ALERT_WINDOW",
    "BIND_DEVICE_ADMIN", "READ_PHONE_STATE", "READ_PHONE_NUMBERS", "PROCESS_OUTGOING_CALLS",
    "MANAGE_EXTERNAL_STORAGE", "BIND_NOTIFICATION_LISTENER_SERVICE",
})


def _short(perm: str) -> str:
    return perm.rsplit(".", 1)[-1].upper()


def android_excess_dangerous(official: list[str], candidate: list[str]) -> list[str]:
    """Dangerous permissions the candidate requests that the official app does not."""
    official_short = {_short(p) for p in official}
    return sorted({
        p for p in candidate
        if _short(p) not in official_short and _short(p) in ANDROID_DANGEROUS
    })


def android_permission_jaccard(official: list[str], candidate: list[str]) -> float:
    if not official:
        return 0.0
    a = {_short(p) for p in official}
    b = {_short(p) for p in candidate}
    return round(len(a & b) / len(a | b), 3)


def _normalise(perm_data: dict | list) -> list[str]:
    """Flatten google_play_scraper permissions() dict → sorted list of uppercased descriptions.

    Input (dict):  {"Storage": ["Read the contents of your USB storage", ...], ...}
    Input (list):  ["READ CONTACTS", ...]  — already flat
    Output:        ["READ THE CONTENTS OF YOUR USB STORAGE", ...]
    """
    out: set[str] = set()
    if isinstance(perm_data, dict):
        for group_perms in perm_data.values():
            for p in (group_perms or []):
                if isinstance(p, str):
                    out.add(p.strip().upper())
    elif isinstance(perm_data, list):
        for p in perm_data:
            if isinstance(p, str):
                out.add(p.strip().upper())
    return sorted(out)


def _fetch_permissions_sync(package_id: str) -> list[str]:
    """Blocking. The permissions the package's Google Play listing shows."""
    try:
        from google_play_scraper import permissions as gps_perms
        data = call_with_backoff(partial(gps_perms, package_id, lang="en"))
        return _normalise(data)
    except Exception as exc:
        # A package that is not on Google Play (third-party/sideloaded apps) is expected: the library then
        # raises NotFoundError or a TypeError from an empty page. Anything else is a real failure.
        level = log.info if isinstance(exc, TypeError) or type(exc).__name__ == "NotFoundError" else log.warning
        level("permissions lookup failed for %r: %s: %s", package_id, type(exc).__name__, exc)
        return []


def _fetch_account_sync(package_id: str, country: str = "us") -> tuple[str | None, str | None]:
    """Blocking. (developer name, developer id) on the package's Google Play listing; None for what it lacks."""
    try:
        from google_play_scraper import app as gps_app
        info = call_with_backoff(partial(gps_app, package_id, lang="en", country=country)) or {}
    except Exception as exc:
        # Not on Play (or Play unreachable): the caller falls back to whatever publishers it was given
        log.info("developer lookup failed for %r: %s: %s", package_id, type(exc).__name__, exc)
        return None, None
    name, dev_id = info.get("developer"), info.get("developerId")
    return (
        (name.strip() or None) if isinstance(name, str) else None,
        (str(dev_id).strip() or None) if dev_id else None,
    )


def _fetch_listing_sync(package_id: str, country: str = "us") -> dict:
    """Blocking. The Google Play listing of the package in one storefront, or {} when it is not there."""
    try:
        from google_play_scraper import app as gps_app
        return call_with_backoff(partial(gps_app, package_id, lang="en", country=country)) or {}
    except Exception as exc:
        log.info("listing lookup failed for %r in %s: %s: %s", package_id, country, type(exc).__name__, exc)
        return {}


# Hosts that anyone can register or rent: a developer's email or privacy page there says nothing about the brand.
_SHARED_HOSTS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com", "protonmail.com",
    "google.com", "sites.google.com", "play.google.com", "apple.com", "apps.apple.com", "facebook.com",
    "medium.com", "github.io", "blogspot.com", "wixsite.com", "weebly.com", "wordpress.com", "notion.site",
    "netlify.app", "vercel.app", "pages.dev", "web.app", "firebaseapp.com", "herokuapp.com", "carrd.co",
})


def listing_icon(listing: dict) -> str | None:
    icon = listing.get("icon")
    return icon if isinstance(icon, str) and icon.startswith("http") else None


def listing_domains(listing: dict) -> list[tuple[str, str]]:
    """The brand's own domains as the developer publishes them on the listing: (domain, field it came from).

    developerWebsite, privacyPolicy and the domain of developerEmail. A domain that is a shared host is skipped.
    Each domain is listed once, under the first field that gives it.
    """
    found: dict[str, str] = {}
    for field in ("developerWebsite", "privacyPolicy", "developerEmail"):
        value = (listing.get(field) or "").strip()
        if field == "developerEmail":
            host = value.rsplit("@", 1)[-1] if "@" in value else ""
        else:
            host = urlparse(value if "://" in value else f"https://{value}").hostname or ""
        domain = registrable_domain(host.lower()) if host else ""
        if not domain or "." not in domain or domain in _SHARED_HOSTS:
            continue
        found.setdefault(domain, field)
    return list(found.items())


async def fetch_permissions(package_id: str) -> list[str]:
    return await asyncio.to_thread(_fetch_permissions_sync, package_id)


async def fetch_listing(package_id: str, countries: list[str] | None = None) -> dict:
    """The package's Google Play listing, read from the first storefront that has the app."""
    for country in countries or ["us"]:
        listing = await asyncio.to_thread(_fetch_listing_sync, package_id, country.lower())
        if listing:
            return listing
    return {}


async def fetch_account(package_id: str, countries: list[str] | None = None) -> tuple[str | None, str | None]:
    """(developer name, developer id) of the package's Google Play listing.

    An app can be missing from some storefronts, so each country is tried in turn until the listing is read.
    """
    for country in countries or ["us"]:
        name, dev_id = await asyncio.to_thread(_fetch_account_sync, package_id, country.lower())
        if name or dev_id:
            return name, dev_id
    return None, None
