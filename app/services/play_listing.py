# app/services/play_listing.py
"""Google Play listing facts used by scoring: who publishes an app.

They come from google_play_scraper, so no APK is downloaded. The blocking calls run in a worker thread through
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
