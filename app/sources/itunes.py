# app/sources/itunes.py
"""Apple iTunes Search API adapter.

Endpoint: https://itunes.apple.com/search
Auth: None required
Rate limit: ~20 calls/minute per IP (no official SLA)
"""

from __future__ import annotations

import datetime
import hashlib
import logging
from urllib.parse import quote_plus

from app.api.models import (
    FoundVia,
    Hit,
    HitType,
    HostInfo,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.services.brand_match import brand_terms
from app.sources.base import SourceAdapter, country_label, scan_countries
from app.utils.http_client import fetch
import json as _json

log = logging.getLogger(__name__)


class ItunesAdapter(SourceAdapter):
    name = "itunes"
    method = SourceMethod.api
    host_group = "official_store"
    max_results = 25  # lookalikes can rank below the brand's own apps; scoring filters the noise

    _BASE = "https://itunes.apple.com/search"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        hits: list[Hit] = []
        seen_ids: set[str] = set()
        error: str | None = None

        # Only search brand-relevant terms — generic words like "billing" produce
        # large false-positive noise from the App Store search ranking.
        terms = brand_terms(ctx, kinds=("original", "lure"), limit=5, variants=2)
        countries = scan_countries(ctx)
        # A bundle id is unique on the App Store only, so an Android package id never marks the official listing
        own_bundles = ctx.request.official_app_ids()["ios"]

        def add_items(items: list, term: str) -> None:  # term is the query label
            for item in items:
                bundle_id = item.get("bundleId", "")
                track_url = item.get("trackViewUrl", "")
                if not track_url:
                    continue
                uid = hashlib.sha256(track_url.encode()).hexdigest()[:32]
                if uid in seen_ids:
                    continue
                seen_ids.add(uid)
                is_official = bool(bundle_id) and bundle_id.strip().lower() in own_bundles
                hits.append(
                    Hit(
                        id=uid,
                        type=HitType.app_listing,
                        title=item.get("trackName"),
                        app_name=item.get("trackName"),
                        package_id=bundle_id,
                        version=item.get("version"),
                        publisher=item.get("sellerName") or item.get("artistName"),
                        developer_id=str(item["artistId"]) if item.get("artistId") else None,
                        url=track_url,
                        host=HostInfo(
                            group=self.host_group,
                            name="Apple App Store",
                            domain="apps.apple.com",
                            is_official_store=True,
                        ),
                        found_via=[FoundVia(source=self.name, method=self.method, query=term)],
                        relevance_score=0,
                        is_official_match=is_official,
                        discovered_at=datetime.datetime.now(datetime.timezone.utc),
                    )
                )

        async def get_json(url: str, label: str) -> dict | None:
            nonlocal error
            try:
                return _json.loads((await fetch(url, rotate_ua=False)).content)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                log.warning("itunes: could not fetch/parse response for %r: %s", label, e)
                return None

        # The official app is looked up directly by bundle id, so it is present for comparison (and its
        # publisher can be learned) even when search does not surface it. Not found => the orchestrator warns.
        if ctx.request.ios_bundle:
            for country in countries:
                url = f"https://itunes.apple.com/lookup?bundleId={quote_plus(ctx.request.ios_bundle)}&country={country}"
                data = await get_json(url, f"lookup {ctx.request.ios_bundle}/{country}")
                if data:
                    add_items(data.get("results", []), country_label(ctx.request.ios_bundle, country, countries))

        for term in terms:
            for country in countries:
                url = (
                    f"{self._BASE}?term={quote_plus(term)}&media=software"
                    f"&entity=software&country={country}&limit=50"
                )
                data = await get_json(url, f"{term} ({country})")
                if data:
                    add_items(data.get("results", []), country_label(term, country, countries))

        return hits, SourceStatus(
            name=self.name,
            method=self.method,
            status="failed" if (error and not hits) else "ok",
            results=len(hits),
            error=error,
        )

