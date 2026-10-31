# app/services/discovery.py
"""Brand discovery: find the apps a brand owns, from a Google Play link, an App Store link, or both.

  Google Play   the package id comes from the link. Its listing gives the developer name and developer id. The
                developer's other apps come from the developer page, because google-play-scraper has no lookup
                for a developer's apps. The page is scrolled in a browser; if it cannot be scrolled to the end,
                the result says the list may be incomplete.
  App Store     the app id comes from the link. Its seller id gives all the seller's software. When only a Play
                link is given, the seller is found by searching the brand terms and keeping the sellers whose name
                matches the Play developer name. That match is by name only, so the customer must confirm it.

Candidates are returned with a `default` flag. A candidate is pre-selected only when its name contains the brand
name or an alias. Nothing here is allowlisted: the customer confirms the list first (see brand_store).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from functools import partial
from typing import Any
from urllib.parse import parse_qs, quote, quote_plus, urlparse

from app.services.allowlist import _norm_publisher as norm_name
from app.utils.browser import collect_while_scrolling
from app.utils.http_client import FetchError, call_with_backoff, decode_response, fetch, throttle

log = logging.getLogger(__name__)

_PLAY_HOST = "play.google.com"
_APP_STORE_HOST = "apps.apple.com"
_PLAY_DEV_URL = "https://play.google.com/store/apps/developer?id={dev}&hl=en&gl={cc}"
_ITUNES_SEARCH = "https://itunes.apple.com/search?term={term}&entity=software&country={cc}&limit=200"
_ITUNES_LOOKUP = "https://itunes.apple.com/lookup?id={aid}&entity=software&country={cc}&limit=200"
_DEV_PACKAGE_RE = re.compile(r"details\?id=([A-Za-z0-9_.]+)")
_APP_ID_RE = re.compile(r"/id(\d+)")
_CONCURRENCY = 3


class DiscoveryError(Exception):
    """A step the discovery cannot continue past (for example, a link is not a listing)."""


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


def name_matches(name: str, terms: list[str]) -> bool:
    """True when the normalised name contains one of the normalised brand terms (brand name or alias)."""
    folded = f" {norm_name(name)} "
    return any(f" {norm_name(t)} " in folded for t in terms if norm_name(t))


async def _play_listing(pkg: str, cc: str) -> dict[str, Any] | None:
    """Play listing for one package, or None when the library cannot read it."""
    from google_play_scraper import app as gp_app

    await throttle(f"https://{_PLAY_HOST}/store/apps/details")
    try:
        return await asyncio.to_thread(call_with_backoff, partial(gp_app, pkg, lang="en", country=cc))
    except Exception as exc:  # the library raises several types for missing or removed apps
        log.info("discovery: play listing %s unavailable: %s", pkg, exc)
        return None


async def _play_developer_packages(dev_id: str, cc: str, notes: list[str]) -> list[str]:
    """Package ids of the developer's apps, in page order, without duplicates.

    The page is scrolled in a browser so that apps loaded after the first screen are read too. When the browser
    is unavailable, the static page is read instead, which lists only the first apps.
    """
    # The developer id is a name slug that keeps its '+' separators (State+Bank+of+India)
    url = _PLAY_DEV_URL.format(dev=quote(dev_id, safe="+"), cc=cc)
    try:
        packages, complete = await collect_while_scrolling(url, _DEV_PACKAGE_RE)
    except Exception as exc:  # Playwright missing or a browser failure: the static page still gives the first apps
        log.info("discovery: browser read of developer page %s failed, using the static page: %s", dev_id, exc)
        html = decode_response(await fetch(url))
        packages = list(dict.fromkeys(_DEV_PACKAGE_RE.findall(html)))
        complete = False
    if not complete:
        notes.append("the Play developer page may not list every app of the developer")
    return packages


async def _itunes_json(url: str) -> dict[str, Any]:
    return json.loads(decode_response(await fetch(url)))


async def _itunes_app(aid: int, cc: str) -> dict[str, Any] | None:
    """The App Store listing for one app id, or None when the lookup does not return it."""
    data = await _itunes_json(_ITUNES_LOOKUP.format(aid=aid, cc=cc))
    return next((r for r in data.get("results", []) if r.get("trackId") == aid), None)


async def _artist_ids_by_name(publisher: str, terms: list[str], cc: str) -> dict[int, None]:
    """Seller ids of the App Store sellers whose name equals the publisher name."""
    artist_ids: dict[int, None] = {}
    target = norm_name(publisher)
    if not target:
        return artist_ids
    for term in terms:
        data = await _itunes_json(_ITUNES_SEARCH.format(term=quote_plus(term), cc=cc))
        for r in data.get("results", []):
            if norm_name(r.get("artistName") or "") == target and r.get("artistId"):
                artist_ids.setdefault(int(r["artistId"]), None)
    return artist_ids


async def _seller_software(artist_ids: dict[int, None], cc: str) -> list[dict[str, Any]]:
    """Software items of each App Store seller."""
    apps: list[dict[str, Any]] = []
    for aid in artist_ids:
        data = await _itunes_json(_ITUNES_LOOKUP.format(aid=aid, cc=cc))
        apps.extend(r for r in data.get("results", []) if r.get("kind") == "software" and r.get("bundleId"))
    return apps


async def _play_candidates(
    play_urls: list[str], cc: str, terms: list[str], notes: list[str],
) -> tuple[list[tuple[str, str]], list[dict[str, Any]]]:
    """Play candidates for the listed apps and their developers.

    Returns ([(developer name, developer id), ...] in link order, candidates). A developer's page is read once,
    however many of its apps were linked.
    """
    listed: dict[str, dict[str, Any]] = {}
    for url in play_urls:
        pkg = play_package_from_url(url)
        if not pkg:
            raise DiscoveryError("not a Google Play details link with an id")
        listing = await _play_listing(pkg, cc)
        if not listing:
            raise DiscoveryError(f"Google Play listing for {pkg} could not be read")
        listed.setdefault(pkg, listing)

    developers: dict[str, str] = {}  # developer id -> developer name
    for listing in listed.values():
        if listing.get("developerId"):
            developers.setdefault(listing["developerId"], listing.get("developer") or "")
    if not developers:
        notes.append("the Play listing has no developer id, so only the listed apps were read")

    play_pkgs = list(listed)
    for dev_id in developers:
        try:
            for other in await _play_developer_packages(dev_id, cc, notes):
                if other not in play_pkgs:
                    play_pkgs.append(other)
        except FetchError as exc:
            log.info("discovery: developer page for %s unavailable: %s", dev_id, exc)
            notes.append("the Play developer page could not be read, so only the listed apps were read")

    sem = asyncio.Semaphore(_CONCURRENCY)

    async def _info(p: str) -> dict[str, Any] | None:
        async with sem:
            return listed[p] if p in listed else await _play_listing(p, cc)

    infos = await asyncio.gather(*[_info(p) for p in play_pkgs])

    candidates: list[dict[str, Any]] = []
    for p, info in zip(play_pkgs, infos):
        if not info:
            continue
        name = info.get("title") or p
        candidates.append({
            "store": "play", "id": p, "name": name, "publisher": info.get("developer") or "",
            "icon": info.get("icon"), "url": f"https://play.google.com/store/apps/details?id={p}",
            "default": name_matches(name, terms), "linked": p in listed,
        })
    return [(name, dev_id) for dev_id, name in developers.items()], candidates


async def _play_search(term: str, cc: str) -> list[dict[str, Any]]:
    """Google Play search results for one term (up to 30). Raises when the search cannot be read."""
    from google_play_scraper import search as gp_search

    await throttle(f"https://{_PLAY_HOST}/store/search")
    return await asyncio.to_thread(call_with_backoff, partial(gp_search, term, lang="en", country=cc, n_hits=30))


async def _name_search_candidates(terms: list[str], cc: str, notes: list[str]) -> list[dict[str, Any]]:
    """Apps found by searching the brand terms on Google Play and the App Store, kept when the title contains a
    brand term. They are not pre-selected: a search also finds apps of other publishers, so the customer picks them."""
    found: list[dict[str, Any]] = []
    for term in terms:
        try:
            hits = await _play_search(term, cc)
        except Exception as exc:  # the library raises several types when a search is blocked or fails
            log.info("discovery: play search %r unavailable: %s", term, exc)
            notes.append(f"the Google Play search for '{term}' could not be read")
            hits = []
        for h in hits:
            pkg, title = h.get("appId"), h.get("title") or ""
            if pkg and name_matches(title, terms):
                found.append({
                    "store": "play", "id": pkg, "name": title, "publisher": h.get("developer") or "",
                    "icon": h.get("icon"), "url": f"https://play.google.com/store/apps/details?id={pkg}",
                    "default": False,
                })
        try:
            data = await _itunes_json(_ITUNES_SEARCH.format(term=quote_plus(term), cc=cc))
        except FetchError:
            notes.append(f"the App Store search for '{term}' could not be read")
            continue
        for r in data.get("results", []):
            name = r.get("trackName") or ""
            if r.get("bundleId") and name_matches(name, terms):
                found.append({
                    "store": "app_store", "id": r["bundleId"], "name": name,
                    "publisher": r.get("artistName") or "", "icon": r.get("artworkUrl60"),
                    "url": r.get("trackViewUrl"), "default": False,
                })
    return found


async def discover(
    play_urls: list[str],
    app_store_urls: list[str],
    brand_name: str,
    aliases: list[str],
    cc: str,
) -> dict[str, Any]:
    """Build the candidate list for one brand from its Play links, App Store links or both.

    Each link is one app. Apps are grouped by developer (Play) and seller (App Store), so one developer or seller
    is read once however many of its apps were linked. Raises DiscoveryError when no link is given, or when a
    given link cannot be read.
    """
    if not play_urls and not app_store_urls:
        raise DiscoveryError("give a Google Play link, an App Store link or both")
    cc = cc.lower()
    terms = [brand_name, *aliases]
    notes: list[str] = []
    candidates: list[dict[str, Any]] = []
    developers: list[tuple[str, str]] = []
    if play_urls:
        developers, play_items = await _play_candidates(play_urls, cc, terms, notes)
        candidates.extend(play_items)

    artist_ids: dict[int, None] = {}
    linked_bundles: set[str] = set()  # the apps behind the App Store links
    seller_name = None
    for url in app_store_urls:
        aid = app_store_id_from_url(url)
        if not aid:
            raise DiscoveryError("not an App Store link with an app id, e.g. https://apps.apple.com/in/app/name/id123")
        seed = await _itunes_app(aid, cc)
        if not seed or not seed.get("artistId"):
            raise DiscoveryError(f"App Store listing {aid} could not be read")
        artist_ids[int(seed["artistId"])] = None
        seller_name = seller_name or seed.get("artistName")
        if seed.get("bundleId"):
            linked_bundles.add(seed["bundleId"])

    for dev_name, _ in developers:
        artist_ids.update(await _artist_ids_by_name(dev_name, terms, cc))
    if play_urls and not app_store_urls:
        notes.append("App Store apps were matched by seller name from the Play developer, so confirm them")
    if app_store_urls and not play_urls:
        notes.append("no Google Play link was given, so Android apps were not read")

    first_dev = developers[0][0] if developers else None
    for app in await _seller_software(artist_ids, cc):
        bundle = app["bundleId"]
        name = app.get("trackName") or bundle
        candidates.append({
            "store": "app_store", "id": bundle, "name": name, "linked": bundle in linked_bundles,
            "publisher": app.get("artistName") or first_dev or seller_name or "",
            "icon": app.get("artworkUrl60"), "url": app.get("trackViewUrl"),
            "default": name_matches(name, terms),
        })

    # Listed apps come first, so a search hit for the same id never replaces them (setdefault keeps the first)
    candidates.extend(await _name_search_candidates(terms, cc, notes))

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for c in candidates:
        unique.setdefault((c["store"], c["id"]), c)

    return {
        "play_package": play_package_from_url(play_urls[0]) if play_urls else None,
        "play_developer": first_dev or seller_name or "",
        "play_developer_id": developers[0][1] if developers else "",
        "candidates": list(unique.values()),
        "notes": notes,
    }


async def listed_app(url: str, cc: str) -> dict[str, str]:
    """The store, id and publisher of the app behind one link, as the store shows them.

    Raises DiscoveryError when the link is not a listing or the store cannot be read.
    """
    pkg = play_package_from_url(url)
    if pkg:
        listing = await _play_listing(pkg, cc.lower())
        if not listing:
            raise DiscoveryError(f"Google Play listing for {pkg} could not be read")
        return {"store": "play", "id": pkg, "publisher": listing.get("developer") or ""}
    aid = app_store_id_from_url(url)
    if not aid:
        raise DiscoveryError(f"{url} is not a store link with an id")
    seed = await _itunes_app(aid, cc.lower())
    if not seed or not seed.get("bundleId"):
        raise DiscoveryError(f"App Store listing {aid} could not be read")
    return {"store": "app_store", "id": seed["bundleId"], "publisher": seed.get("artistName") or ""}
