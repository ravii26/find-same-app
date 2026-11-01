# app/services/discovery.py
"""Reading the brand's own store links.

listed_app() reads the app behind one Google Play or App Store link at scan time: its store, id, publisher and
publishing account. Scans read every link the brand record holds, and services/allowlist.py clears those apps.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from functools import partial
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.utils.http_client import call_with_backoff, decode_response, fetch, throttle

log = logging.getLogger(__name__)

_PLAY_HOST = "play.google.com"
_APP_STORE_HOST = "apps.apple.com"
_ITUNES_LOOKUP = "https://itunes.apple.com/lookup?id={aid}&entity=software&country={cc}&limit=200"
_APP_ID_RE = re.compile(r"/id(\d+)")


class DiscoveryError(Exception):
    """A store link that is not a listing, or a listing the store cannot be read for."""


def play_package_from_url(url: str) -> str | None:
    """The package id in a Play Store details link, or None when the link is not one."""
    parts = urlparse(url.strip())
    if parts.hostname != _PLAY_HOST or not parts.path.startswith("/store/apps/details"):
        return None
    ids = parse_qs(parts.query).get("id") or []
    pkg = ids[0].strip() if ids else ""
    return pkg if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+", pkg) else None


def app_store_id_from_url(url: str) -> int | None:
    """The numeric app id in an App Store link (apps.apple.com/<country>/app/<name>/id<number>), or None."""
    parts = urlparse(url.strip())
    if parts.hostname != _APP_STORE_HOST:
        return None
    match = _APP_ID_RE.search(parts.path)
    return int(match.group(1)) if match else None


async def _play_listing(pkg: str, cc: str) -> dict[str, Any] | None:
    """Play listing for one package, or None when the library cannot read it."""
    from google_play_scraper import app as gp_app

    await throttle(f"https://{_PLAY_HOST}/store/apps/details")
    try:
        return await asyncio.to_thread(call_with_backoff, partial(gp_app, pkg, lang="en", country=cc))
    except Exception as exc:  # the library raises several types for missing or removed apps
        log.info("discovery: play listing %s unavailable: %s", pkg, exc)
        return None


async def _itunes_app(aid: int, cc: str) -> dict[str, Any] | None:
    """The App Store listing for one app id, or None when the lookup does not return it."""
    data = json.loads(decode_response(await fetch(_ITUNES_LOOKUP.format(aid=aid, cc=cc))))
    return next((r for r in data.get("results", []) if r.get("trackId") == aid), None)


async def listed_app(url: str, cc: str) -> dict[str, str]:
    """The store, id, publisher and publishing account (developer_id) of the app behind one link, as the store
    shows them. developer_id is the Play developer id or the App Store artistId, "" when the store gives none.

    Raises DiscoveryError when the link is not a listing or the store cannot be read.
    """
    pkg = play_package_from_url(url)
    if pkg:
        listing = await _play_listing(pkg, cc.lower())
        if not listing:
            raise DiscoveryError(f"Google Play listing for {pkg} could not be read")
        return {"store": "play", "id": pkg, "publisher": listing.get("developer") or "",
                "developer_id": str(listing.get("developerId") or "")}
    aid = app_store_id_from_url(url)
    if not aid:
        raise DiscoveryError(f"{url} is not a store link with an id")
    seed = await _itunes_app(aid, cc.lower())
    if not seed or not seed.get("bundleId"):
        raise DiscoveryError(f"App Store listing {aid} could not be read")
    return {"store": "app_store", "id": seed["bundleId"], "publisher": seed.get("artistName") or "",
            "developer_id": str(seed.get("artistId") or "")}
