# app/services/allowlist.py
"""Allowlist: keep the brand's own apps, domains and publishers out of the rogue results.

Nothing is guessed. Each hit is checked in this order, and the first rule that matches clears it:

  1. App Store artist id   an App Store hit whose developer_id (artistId) is the artistId of the App Store URL the
                           caller gave. Only App Store hits are checked by this rule.
  2. Package and publisher the hit's package_id and publisher match its own store's metadata, read from the store
                           for the caller's URL:
                             Google Play  package_id and publisher match one Play listing the caller gave
                                          (android_packages): the package and its developer name
                             App Store    package_id and publisher match one App Store listing the caller gave
                                          (ios_bundles): the bundle and its seller name
                           Each store is compared only with its own metadata. A copy on an APK site or any other store
                           is never cleared by this rule, because it can state the same publisher and package itself.
  3. Play developer id     a Google Play hit whose developer_id is one of the brand's play_developer_ids.
  4. Official domain       a web hit whose URL's registrable domain is official_domain or one of official_domains.
  5. Brand's Play link     a search result that links to a Google Play package the brand owns.

Every allowlisted hit is reported with its reason in `ScanReport.allowlisted` for audit.
"""

from __future__ import annotations

import re

from app.api.models import AllowlistedItem, Hit, ScanRequest
from app.utils.url_tools import registrable_domain

_CORP_SUFFIXES = frozenset({
    "inc", "llc", "ltd", "limited", "pvt", "private", "co", "corp", "corporation", "company",
    "gmbh", "ag", "sa", "plc", "lp", "llp", "pte", "bv", "nv", "ab", "oy", "srl", "spa", "pty",
})


def _norm_publisher(name: str | None) -> str:
    """Casefold, drop punctuation and corporate suffixes: 'Google LLC' == 'Google Inc.'"""
    words = re.sub(r"[^\w\s]", " ", (name or "").casefold()).split()
    return " ".join(w for w in words if w not in _CORP_SUFFIXES)


def publisher_matches(name: str | None, publishers: set[str] | frozenset[str]) -> bool:
    """Does a listing's publisher name equal one of the brand's (normalised)?

    Some stores cut long names short ("Paytm - One97 Communications L" for "... Communications Ltd."). A name whose
    last word is the start of a corporate suffix is compared without it, when at least two words remain.
    """
    norm = _norm_publisher(name)
    if not norm or not publishers:
        return False
    if norm in publishers:
        return True
    words = norm.split()
    if len(words) >= 3 and any(suffix.startswith(words[-1]) for suffix in _CORP_SUFFIXES):
        return " ".join(words[:-1]) in publishers
    return False


def _official_ids(request: ScanRequest) -> dict[str, set[str]]:
    """The brand's own app ids per platform (see ScanRequest.official_app_ids)."""
    return request.official_app_ids()


def _platform(hit: Hit) -> str:
    """'ios' for App Store hits, 'android' for everything else (Google Play, APK sites, other Android stores)."""
    domain = getattr(hit.host, "domain", None) if hit.host else None
    return "ios" if domain == "apps.apple.com" else "android"


def _domains(request: ScanRequest) -> set[str]:
    return {
        registrable_domain(d).lower()
        for d in [request.official_domain, *request.official_domains] if d
    }


PLAY_DOMAIN = "play.google.com"
APP_STORE_DOMAIN = "apps.apple.com"


def official_accounts(request: ScanRequest) -> dict[str, set[str]]:
    """The brand's Google Play developer ids, keyed by the store's domain. App Store artist ids come from the URL."""
    return {PLAY_DOMAIN: set(request.play_developer_ids)}


def official_publishers(request: ScanRequest) -> set[str]:
    """Normalised publisher names the caller declared (`official_publishers`)."""
    pubs = {_norm_publisher(p) for p in request.official_publishers}
    pubs.discard("")
    return pubs


def _id(value: object) -> str | None:
    """A store developer id as text: Play gives text or numbers, the App Store numbers."""
    text = str(value).strip() if value is not None else ""
    return text or None


def store_metadata(
    play_listings: list[tuple[str | None, str | None, object]] = (),
    app_store_listings: list[tuple[str | None, str | None, object]] = (),
) -> dict[str, dict]:
    """The brand's own listings, read from the store for each Play package and App Store bundle the caller gave.

    A Play listing is (package, developer name, developer id). An App Store listing is (bundle, seller name,
    artist id). Each listing is kept as one pair, so a hit clears only against the listing it belongs to. The developer
    ids of all listings of a store are collected in seller_ids. A store with nothing read is absent.
    """
    meta: dict[str, dict] = {}
    for domain, listings in ((PLAY_DOMAIN, play_listings), (APP_STORE_DOMAIN, app_store_listings)):
        pairs = [{"package": pkg.strip().lower(), "publisher": _norm_publisher(pub)}
                 for pkg, pub, _ in listings if pkg and pub and _norm_publisher(pub)]
        ids = {_id(dev) for _, _, dev in listings} - {None}
        if pairs or ids:
            meta[domain] = {"listings": pairs, "seller_ids": ids}
    return meta


def allowlist_reason(
    hit: Hit, official_ids: dict[str, set[str]], domains: set[str],
    store_meta: dict[str, dict] | None = None,
) -> str | None:
    """Why this hit is allowlisted, or None. The rules run in the order given in the module docstring."""
    domain = getattr(hit.host, "domain", None) if hit.host else None
    developer_id = _id(getattr(hit, "developer_id", None))
    meta = store_meta or {}

    # 1. Developer id (Play developer id, App Store artist id), from the URL the caller gave, on the hit's own store
    if developer_id and developer_id in meta.get(domain or "", {}).get("seller_ids", set()):
        return f"published by the brand's {hit.host.name} account {developer_id}"

    pkg = (hit.package_id or "").strip().lower()
    if pkg:
        # 2. Package and publisher, against the hit's own store only
        if any(
            listing["package"] == pkg and publisher_matches(hit.publisher, {listing["publisher"]})
            for listing in meta.get(domain or "", {}).get("listings", [])
        ):
            return f"package {hit.package_id} and publisher '{hit.publisher}' match the brand's {hit.host.name} listing"
        return None

    if domains and registrable_domain(hit.url).lower() in domains:
        return f"domain {registrable_domain(hit.url)} is an official domain"

    # A search result that links to the brand's own Google Play listing (Bing returns these for the brand's name).
    # Google Play serves a package only to its owner's listing, so the link itself is the brand's.
    from app.services.discovery import play_package_from_url  # discovery imports this module

    linked = (play_package_from_url(hit.url) or "").lower()
    if linked and linked in official_ids.get("android", set()):
        return f"link to the brand's own Google Play listing ({linked})"

    return None


def split_allowlisted(
    hits: list[Hit], request: ScanRequest, keep_official: bool = True,
    store_meta: dict[str, dict[str, set[str]]] | None = None,
) -> tuple[list[Hit], list[AllowlistedItem]]:
    """Return (remaining hits, allowlisted items).

    Official-listing hits (`is_official_match`) are kept in `remaining` when
    keep_official is True so icon comparison can still use them; the
    caller drops them after scoring with `drop_official`.
    """
    official_ids = _official_ids(request)
    domains, accounts = _domains(request), official_accounts(request)

    # An app the store shows under one of the brand's accounts is the brand's own: its package id then counts as an
    # official id, so the same package on another store or APK site is allowlisted too — still only when that
    # listing's publisher matches as well (a copy re-uploaded under another name stays a finding).
    account_ids = _account_verified_ids(hits, accounts)
    official_ids = {p: official_ids.get(p, set()) | account_ids.get(p, set()) for p in ("android", "ios")}

    remaining: list[Hit] = []
    allowed: list[AllowlistedItem] = []
    for h in hits:
        if keep_official and h.is_official_match:
            remaining.append(h)
            continue
        reason = allowlist_reason(h, official_ids, domains, store_meta)
        if reason:
            allowed.append(_item(h, reason))
        else:
            remaining.append(h)
    return remaining, allowed


def _account_verified_ids(hits: list[Hit], accounts: dict[str, set[str]]) -> dict[str, set[str]]:
    """Package ids, per platform, of the hits the store shows under one of the brand's accounts."""
    ids: dict[str, set[str]] = {"android": set(), "ios": set()}
    for h in hits:
        domain = getattr(h.host, "domain", None) if h.host else None
        developer_id = getattr(h, "developer_id", None)
        if h.package_id and developer_id and domain and developer_id in accounts.get(domain, set()):
            ids[_platform(h)].add(h.package_id.strip().lower())
    return ids


def drop_official(hits: list[Hit]) -> tuple[list[Hit], list[AllowlistedItem]]:
    """Remove official-listing hits after scoring; report them as allowlisted."""
    kept = [h for h in hits if not h.is_official_match]
    dropped = [_item(h, "official app listing") for h in hits if h.is_official_match]
    return kept, dropped


def audit_item(hit: Hit, reason: str) -> AllowlistedItem:
    """Audit-trail entry for a hit that was removed from the results, with the reason."""
    return _item(hit, reason)


def _item(hit: Hit, reason: str) -> AllowlistedItem:
    return AllowlistedItem(
        url=hit.url,
        app_name=hit.app_name or hit.title,
        package_id=hit.package_id,
        publisher=hit.publisher,
        developer_id=hit.developer_id,
        source=hit.host.name if hit.host else None,
        reason=reason,
    )


