# app/sources/aptoide_adapter.py
"""Aptoide public API adapter.

Endpoint: https://ws75.aptoide.com/api/7/apps/search/query={query}/limit={n}
Auth: None required for anonymous search (access_token optional)
Rate limit: ~1 req/sec (conservative)

Returns JSON with package, name, developer, malware.rank, etc.

getMeta cert shortcut: for each hit, calls ws75.aptoide.com/api/7/app/getMeta
to retrieve the signing certificate SHA-1 without downloading the APK.
"""

from __future__ import annotations

import hashlib
import json as _json
import logging

from app.api.models import (
    Hit,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.services.brand_match import brand_terms
from app.sources.base import SourceAdapter, scraped_hit
from app.utils.http_client import FetchError, fetch

log = logging.getLogger(__name__)




class AptoideAdapter(SourceAdapter):
    name = "aptoide"
    method = SourceMethod.api
    host_group = "apk_site"

    _BASE = "https://ws75.aptoide.com/api/7/apps/search"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        # Only search for brand-relevant terms — generic words like "billing" or
        # "restaurant" produce huge false-positive noise on Aptoide.
        terms = brand_terms(ctx, kinds=("original", "package"), limit=4, variants=3)
        hits: list[Hit] = []
        seen: set[str] = set()
        error: str | None = None

        for term in terms:
            url = f"{self._BASE}/query={term}/limit=25"
            try:
                resp = await fetch(url, rotate_ua=False)
                data = _json.loads(resp.content)
            except FetchError as e:
                error = str(e)
                log.warning("aptoide: request failed for query %r: %s", term, e)
                continue
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                log.warning("aptoide: could not parse response for query %r: %s", term, e)
                continue

            # .get(k, {}) only defaults on a missing key — Aptoide error
            # responses set the key to null (e.g. {"datalist": null}), so guard
            # every nested lookup with or {} to stay crash-safe under load.
            for item in (data.get("datalist") or {}).get("list") or []:
                pkg = item.get("package", "")
                listing_url = (item.get("urls") or {}).get("w", "")
                if not listing_url:
                    listing_url = f"https://aptoide.com/app/{pkg}" if pkg else ""
                if not listing_url:
                    continue

                uid = hashlib.sha256(listing_url.encode()).hexdigest()[:32]
                if uid in seen:
                    continue
                seen.add(uid)

                file_obj = item.get("file") or {}

                hits.append(
                    scraped_hit(
                        self,
                        term,
                        url=listing_url,
                        host_name="Aptoide",
                        host_domain="aptoide.com",
                        hit_id=uid,
                        title=item.get("name"),
                        app_name=item.get("name"),
                        package_id=pkg,
                        version=file_obj.get("vername"),
                        publisher=(item.get("developer") or {}).get("name"),
                        icon_url=item.get("icon") or None,
                        apk_url=file_obj.get("path") or file_obj.get("path_alt") or None,
                    )
                )

        return hits, SourceStatus(
            name=self.name, method=self.method,
            status="failed" if (error and not hits) else "ok",
            results=len(hits),
            error=error,
        )
