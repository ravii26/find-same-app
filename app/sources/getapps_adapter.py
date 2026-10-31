# app/sources/getapps_adapter.py
"""GetApps (Xiaomi Global App Store) adapter.

REST API: https://global.app.mi.com/intl/web/api/search
Auth:     None — anonymous public API
Rate:     Conservative; 1 req/sec default
Coverage: Xiaomi/MIUI ecosystem — large India/SEA market share where rogue apps surface frequently.
"""

from __future__ import annotations

import datetime
import hashlib
import json as _json
import logging
from urllib.parse import urlencode

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
from app.utils.http_client import FetchError, fetch

log = logging.getLogger(__name__)

_BASE = "https://global.app.mi.com/intl/web/api/search"
_HEADERS = {
    "Referer": "https://global.app.mi.com/",
}


class GetAppsAdapter(SourceAdapter):
    name = "getapps"
    method = SourceMethod.api
    host_group = "app_store"
    max_results = 10  # official store — expect 1 official app + a few lookalikes

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        terms = brand_terms(ctx, kinds=("original", "package"), limit=4, variants=2)

        countries = scan_countries(ctx)
        hits: list[Hit] = []
        seen: set[str] = set()
        error: str | None = None

        for term in terms:
            for country in countries:
                qs = urlencode({"keyword": term, "lo": country.upper(), "la": "en"})
                url = f"{_BASE}?{qs}"
                try:
                    resp = await fetch(url, headers=_HEADERS, rotate_ua=True)
                    data = _json.loads(resp.content)
                except FetchError as e:
                    error = str(e)
                    log.warning("getapps: request failed for query %r: %s", term, e)
                    continue
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"
                    log.warning("getapps: could not parse response for query %r: %s", term, e)
                    continue

                for section in data.get("list", []):
                    for item in section.get("data", {}).get("listApp", []):
                        pkg = item.get("packageName", "")
                        if not pkg:
                            continue
                        listing_url = f"https://global.app.mi.com/apps/details?id={pkg}"
                        uid = hashlib.sha256(listing_url.encode()).hexdigest()[:32]
                        if uid in seen:
                            continue
                        seen.add(uid)

                        icon_path = item.get("icon", "")
                        icon_url = (
                            f"https://fgb0.market.xiaomi.com/download/{icon_path}"
                            if icon_path else None
                        )

                        hits.append(Hit(
                            id=uid,
                            type=HitType.app_listing,
                            title=item.get("displayName"),
                            app_name=item.get("displayName"),
                            package_id=pkg,
                            publisher=item.get("developerName"),
                            icon_url=icon_url,
                            url=listing_url,
                            host=HostInfo(
                                group=self.host_group,
                                name="GetApps (Xiaomi)",
                                domain="global.app.mi.com",
                                is_official_store=True,
                            ),
                            found_via=[FoundVia(source=self.name, method=self.method, query=country_label(term, country, countries))],
                            relevance_score=0,
                            discovered_at=datetime.datetime.now(datetime.timezone.utc),
                        ))

        return hits, SourceStatus(
            name=self.name, method=self.method,
            status="failed" if (error and not hits) else "ok",
            results=len(hits),
            error=error,
        )