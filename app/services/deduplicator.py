# app/services/deduplicator.py
"""Hit deduplication — by normalized URL and package ID.

Within a scan, merge duplicate hits from multiple sources into one,
combining found_via lists.
"""

from __future__ import annotations

from app.api.models import AllowlistedItem, Hit
from app.utils.url_tools import url_id


def deduplicate(hits: list[Hit]) -> list[Hit]:
    """
    Deduplicate hits by normalized URL, and by (store, package ID).

    The package key includes the store: the same package ID on Google Play and on an APK mirror are two
    different listings, and a repackaged copy keeps the official package ID — merging them would hide
    exactly the copy we need to inspect. Within one store, the same package found by several queries
    is one listing.

    When duplicates are found, the first occurrence is kept and subsequent
    occurrences merge their found_via into the first.
    """
    by_url_id: dict[str, int] = {}       # url_id -> index in result
    by_pkg: dict[tuple[str, str], int] = {}  # (store domain, package_id) -> index in result
    result: list[Hit] = []

    for hit in hits:
        uid = url_id(hit.url)
        pkg = (
            ((hit.host.domain if hit.host else "").lower(), hit.package_id.lower().strip())
            if hit.package_id else None
        )

        existing_idx: int | None = None
        if uid in by_url_id:
            existing_idx = by_url_id[uid]
        elif pkg and pkg in by_pkg:
            existing_idx = by_pkg[pkg]

        if existing_idx is not None:
            # Merge found_via into existing hit; preserve is_official_match if
            # any source identified this as official (e.g. iTunes vs Aptoide).
            existing = result[existing_idx]
            merged_via = list(existing.found_via)
            for fv in hit.found_via:
                if not any(f.source == fv.source and f.query == fv.query for f in merged_via):
                    merged_via.append(fv)
            result[existing_idx] = existing.model_copy(update={
                "found_via": merged_via,
                "is_official_match": existing.is_official_match or hit.is_official_match,
                "apk_url": existing.apk_url or hit.apk_url,
                # Keep fields the first listing lacked
                "publisher": existing.publisher or hit.publisher,
                "developer_id": existing.developer_id or hit.developer_id,
                "icon_url": existing.icon_url or hit.icon_url,
            })
        else:
            idx = len(result)
            by_url_id[uid] = idx
            if pkg:
                by_pkg[pkg] = idx
            result.append(hit)

    return result


def group_mirrors(hits: list[Hit]) -> tuple[list[Hit], list[AllowlistedItem]]:
    """Collapse one app listed on several mirror sites into its highest-scoring listing.

    The same package on Aptoide, Uptodown and APKPure is one finding, not three. The kept hit says where
    else it is listed and the others are audited, so nothing is lost silently.
    """
    groups: dict[tuple[str, str], list[Hit]] = {}
    for hit in hits:
        pkg = (hit.package_id or "").strip().lower()
        if pkg and hit.type.value == "app_listing":
            # An App Store bundle id and an Android package id that read the same are two different apps
            platform = "ios" if hit.host and hit.host.domain == "apps.apple.com" else "android"
            groups.setdefault((platform, pkg), []).append(hit)

    merged_ids: set[str] = set()
    replacements: dict[str, Hit] = {}
    audit: list[AllowlistedItem] = []
    for members in groups.values():
        sites = {h.host.name if h.host else h.url for h in members}
        if len(members) < 2 or len(sites) < 2:
            continue
        ranked = sorted(members, key=lambda h: h.relevance_score, reverse=True)
        primary, rest = ranked[0], ranked[1:]
        others = sorted({h.host.name for h in rest if h.host and (not primary.host or h.host.name != primary.host.name)})
        note = f"same package also listed on {len(rest)} other page(s): {', '.join(others) or 'other sites'}"
        replacements[primary.id] = primary.model_copy(update={
            "reasons": primary.reasons + [note],
            "found_via": primary.found_via + [fv for h in rest for fv in h.found_via],
        })
        for h in rest:
            merged_ids.add(h.id)
            audit.append(AllowlistedItem(
                url=h.url, app_name=h.app_name or h.title, package_id=h.package_id, publisher=h.publisher,
                source=h.host.name if h.host else None,
                reason=f"duplicate of the listing on {primary.host.name if primary.host else primary.url} (same package)",
            ))
    kept = [replacements.get(h.id, h) for h in hits if h.id not in merged_ids]
    return kept, audit
