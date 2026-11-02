"""Aptoide store search adapter: the same request the aptoide.com search page makes.

The store page loads its results from ws2-cache.aptoide.com. Request: the store's search with aab=1 (Android App
Bundle support, which the store's ordering needs) and language=en_US (English names). The q value is the store's
device spec, base64 of "myCPU=arm64-v8a,armeabi-v7a,armeabi&leanback=0".
"""

from __future__ import annotations

import hashlib
import json as _json
import logging
from urllib.parse import quote_plus

from app.api.models import (
    Hit,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.services.brand_match import brand_terms, presence_any
from app.sources.base import SourceAdapter, scraped_hit
from app.utils.http_client import FetchError, decode_response, fetch, throttle

log = logging.getLogger(__name__)

_DEVICE_SPEC = "bXlDUFU9YXJtNjQtdjhhLGFybWVhYmktdjdhLGFybWVhYmkmbGVhbmJhY2s9MA"  # base64 of the device spec above
_META = "https://ws2-cache.aptoide.com/api/7/app/getMeta"
# App pages read per scan: one request each (Aptoide asks for a crawl delay of 5 seconds in robots.txt).
_META_LIMIT = 10


class AptoideStoreAdapter(SourceAdapter):
    name = "aptoide_store"
    method = SourceMethod.api
    host_group = "apk_site"

    _BASE = "https://ws2-cache.aptoide.com/api/7/apps/search"

    def _url(self, term: str) -> str:
        return (
            f"{self._BASE}?cdn=web&q={_DEVICE_SPEC}&aab=1&language=en_US"
            f"&query={quote_plus(term)}&limit=20&offset=0&origin=SITE&store_name=aptoide-web"
        )

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        terms = brand_terms(ctx, kinds=("original", "package"), limit=4, variants=3)
        hits: list[Hit] = []
        seen: set[str] = set()
        error: str | None = None

        brands = [ctx.request.brand_name, *ctx.request.brand_aliases]

        for term in terms:
            try:
                resp = await fetch(self._url(term), rotate_ua=False)
                data = _json.loads(resp.content)
            except FetchError as e:
                error = str(e)
                log.warning("aptoide_store: request failed for query %r: %s", term, e)
                continue
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                log.warning("aptoide_store: could not parse response for query %r: %s", term, e)
                continue

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

                if presence_any(brands, item.get("name"), pkg, (item.get("developer") or {}).get("name")) is None:
                    continue  # the search returns any app that matches the keyword: keep the brand only

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

        # The app's own record (what the store's page shows) gives the description, version and seller.
        hits = [await self._with_meta(h) for h in hits[:_META_LIMIT]] + hits[_META_LIMIT:]

        return hits, SourceStatus(
            name=self.name, method=self.method,
            status="failed" if (error and not hits) else "ok",
            results=len(hits),
            error=error,
        )

    @staticmethod
    async def _with_meta(hit: Hit) -> Hit:
        """The hit with description, version and seller from the app's getMeta record. A failed call keeps the search fields."""
        if not hit.package_id:
            return hit
        url = f"{_META}?cdn=web&q={_DEVICE_SPEC}&aab=1&language=en_US&package_name={quote_plus(hit.package_id)}"
        try:
            await throttle(url)
            data = _json.loads(decode_response(await fetch(url, rotate_ua=False)))
        except Exception as exc:  # a missing record or a bad reply keeps the search fields
            log.info("aptoide_store: app record not read for %s: %s", hit.package_id, exc)
            return hit
        meta = data.get("data") or {}
        update: dict[str, str] = {}
        if ((meta.get("media") or {}).get("description") or "").strip():
            update["description"] = meta["media"]["description"].strip()[:1000]  # same cap as the other stores
        if ((meta.get("file") or {}).get("vername") or "").strip():
            update["version"] = meta["file"]["vername"].strip()
        if not hit.publisher and ((meta.get("developer") or {}).get("name") or "").strip():
            update["publisher"] = meta["developer"]["name"].strip()
        return hit.model_copy(update=update) if update else hit
