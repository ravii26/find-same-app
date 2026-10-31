# app/services/allowlist.py
"""Allowlist: keep the brand's own apps, domains and publishers out of the rogue results.

Nothing is guessed. An app is allowlisted only when BOTH of these match what the caller declared:

  publisher  the hit's publisher equals an `official_publishers` entry (case, punctuation and corporate
             suffixes ignored).
  package    the hit's package_id is one of the brand's official ids for the hit's platform (`android_package` and
             `android_packages` for Android hits; `ios_bundle` and `ios_bundles` for App Store hits), or an
             `allowlist_packages` entry. A package id is unique per store, so an iOS bundle never counts on
             Google Play and an Android id never counts on the App Store.

The store the hit was found on does not count, and neither do its URL or its domain. A third-party copy that
keeps both the name and the id is decided by the signing certificate (split_cert_verified), not here.

Non-app hits (web pages) keep one rule:
  domain     the URL's registrable domain is `official_domain` or one of `official_domains`.

Sister apps (another app by the same company, e.g. a business app) need their id in `allowlist_packages`
and the company name in `official_publishers`.
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


def _declared_packages(request: ScanRequest) -> set[str]:
    """Package ids the caller declared as legitimate (sister apps), on any platform.

    The brand's own ids are excluded: they are checked per platform (see _official_ids), so a copy
    on the wrong platform never matches them.
    """
    own = set().union(*_official_ids(request).values())
    return {p.strip().lower() for p in request.allowlist_packages if p} - own


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


def _on_official_store(hit: Hit) -> bool:
    # The source sets is_official_store. A host group is only a label (app_store, official_store, apk_site),
    # so a third-party store must not become official just by sharing a group name.
    return bool(hit.host and hit.host.is_official_store)


def official_publishers(request: ScanRequest) -> set[str]:
    """Normalised publisher names the caller declared (`official_publishers`)."""
    pubs = {_norm_publisher(p) for p in request.official_publishers}
    pubs.discard("")
    return pubs


def allowlist_reason(
    hit: Hit, declared: set[str], official_ids: dict[str, set[str]], domains: set[str], publishers: set[str],
) -> str | None:
    """Why this hit is allowlisted, or None.

    An app (a hit with a package id) is allowlisted only when its publisher and its package id both match
    what the caller declared. The store it was found on, its URL and its domain do not count. A repackaged
    copy keeps the id but not the signing certificate, so split_cert_verified handles those.
    Other hits (web pages) keep the domain rule.
    """
    pkg = (hit.package_id or "").strip().lower()
    if pkg:
        own = official_ids.get(_platform(hit), set())
        if publishers and _norm_publisher(hit.publisher) in publishers and pkg in (declared | own):
            return f"package {hit.package_id} and publisher '{hit.publisher}' match the declared app"
        return None

    if domains and registrable_domain(hit.url).lower() in domains:
        return f"domain {registrable_domain(hit.url)} is an official domain"

    return None


def split_allowlisted(
    hits: list[Hit], request: ScanRequest, keep_official: bool = True,
) -> tuple[list[Hit], list[AllowlistedItem]]:
    """Return (remaining hits, allowlisted items).

    Official-listing hits (`is_official_match`) are kept in `remaining` when
    keep_official is True so icon/permission comparison can still use them; the
    caller drops them after scoring with `drop_official`.
    """
    declared, official_ids = _declared_packages(request), _official_ids(request)
    domains, publishers = _domains(request), official_publishers(request)

    remaining: list[Hit] = []
    allowed: list[AllowlistedItem] = []
    for h in hits:
        if keep_official and h.is_official_match:
            remaining.append(h)
            continue
        reason = allowlist_reason(h, declared, official_ids, domains, publishers)
        if reason:
            allowed.append(_item(h, reason))
        else:
            remaining.append(h)
    return remaining, allowed


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
        source=hit.host.name if hit.host else None,
        reason=reason,
    )


def split_cert_verified(hits: list[Hit], request: ScanRequest) -> tuple[list[Hit], list[AllowlistedItem]]:
    """Move third-party copies of the official package whose signing certificate MATCHES the official one.

    Same package id and same certificate: it is the genuine app on a mirror, not a rogue copy.
    (Different certificate stays — that is the repackaging signal.)
    """
    official = (request.android_package or "").lower()
    kept: list[Hit] = []
    verified: list[AllowlistedItem] = []
    for h in hits:
        if (
            official
            and (h.package_id or "").lower() == official
            and h.apk_analysis and h.apk_analysis.cert_match is True
            and not _on_official_store(h)
        ):
            verified.append(audit_item(h, "same package and signing certificate as the official app (third-party mirror)"))
        else:
            kept.append(h)
    return kept, verified