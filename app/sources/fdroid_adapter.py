# app/sources/fdroid_adapter.py
"""F-Droid index adapter.

The full index (index-v2.json, tens of MB) takes over a minute to download and parse, so it is
reduced to a slim {package: {name, version}} map, cached on disk for 24 hours and in memory for
the life of the process. A failed refresh falls back to a stale disk copy rather than failing.

Index URL: https://f-droid.org/repo/index-v2.json
Auth: None
Rate limit: None published; at most one download per day.
"""

from __future__ import annotations

import hashlib
import json as _json
import logging
import time
from pathlib import Path
from typing import ClassVar

from app.api.models import (
    Hit,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.config.settings import get_settings
from app.services.keyword_expansion import fold_homoglyphs
from app.sources.base import SourceAdapter, scraped_hit
from app.utils.http_client import FetchError, fetch

log = logging.getLogger(__name__)


class FDroidAdapter(SourceAdapter):
    name = "fdroid"
    method = SourceMethod.api
    host_group = "app_store"
    max_results = 10  # curated open-source index — low rogue yield

    _INDEX_URL = "https://f-droid.org/repo/index-v2.json"
    _TTL_SECONDS = 24 * 3600
    _index_cache: ClassVar[dict | None] = None
    _index_loaded_at: ClassVar[float] = 0.0

    @staticmethod
    def _cache_path() -> Path:
        return get_settings().data_dir / "cache" / "fdroid-slim.json"

    @staticmethod
    def _slim(full: dict) -> dict:
        out: dict[str, dict] = {}
        for pkg_name, pkg_data in full.get("packages", {}).items():
            name_map = (pkg_data.get("metadata") or {}).get("name") or {}
            name = name_map.get("en-US") or next(iter(name_map.values()), pkg_name)
            versions = pkg_data.get("versions") or {}
            latest = max(
                versions.values(), key=lambda v: v.get("manifest", {}).get("versionCode", 0), default={}
            )
            out[pkg_name] = {"name": name, "version": latest.get("manifest", {}).get("versionName")}
        return out

    async def _get_index(self) -> dict:
        cls = FDroidAdapter
        if cls._index_cache is not None and time.time() - cls._index_loaded_at < cls._TTL_SECONDS:
            return cls._index_cache

        path = cls._cache_path()
        disk: dict | None = None
        if path.exists():
            try:
                disk = _json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                disk = None
            if disk is not None and time.time() - path.stat().st_mtime < cls._TTL_SECONDS:
                cls._index_cache, cls._index_loaded_at = disk, time.time()
                return disk

        try:
            resp = await fetch(self._INDEX_URL, rotate_ua=False, max_bytes=200 * 1024 * 1024, timeout=180)
            slim = self._slim(_json.loads(resp.content))
        except Exception as exc:
            if disk is not None:  # stale beats nothing
                log.warning("fdroid: refresh failed (%s); using stale cache", exc)
                cls._index_cache, cls._index_loaded_at = disk, time.time()
                return disk
            raise
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(_json.dumps(slim), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            log.warning("fdroid: could not write cache: %s", exc)
        cls._index_cache, cls._index_loaded_at = slim, time.time()
        return slim

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        try:
            index = await self._get_index()
        except FetchError as exc:
            log.warning("fdroid: index download failed: %s", exc)
            return [], SourceStatus(
                name=self.name, method=self.method,
                status="failed", error=str(exc),
            )
        except Exception as exc:
            # e.g. JSON decode failure (undecompressed body) — surface it, don't swallow.
            log.warning("fdroid: index parse failed: %s", exc)
            return [], SourceStatus(
                name=self.name, method=self.method,
                status="failed", error=f"{type(exc).__name__}: {exc}",
            )

        search_terms = [
            fold_homoglyphs(kw.term)
            for kw in ctx.expanded_keywords
            if kw.type.value in ("original", "package")
        ]
        brand_fold = fold_homoglyphs(ctx.request.brand_name)

        hits: list[Hit] = []
        for pkg_name, info in index.items():
            name = info.get("name") or pkg_name
            if brand_fold not in fold_homoglyphs(name) and not any(
                t in fold_homoglyphs(pkg_name) for t in search_terms
            ):
                continue

            listing_url = f"https://f-droid.org/en/packages/{pkg_name}/"
            uid = hashlib.sha256(listing_url.encode()).hexdigest()[:32]

            hits.append(
                scraped_hit(
                    self,
                    brand_fold,
                    url=listing_url,
                    host_name="F-Droid",
                    host_domain="f-droid.org",
                    hit_id=uid,
                    title=name,
                    app_name=name,
                    package_id=pkg_name,
                    version=info.get("version"),
                )
            )

        return hits, SourceStatus(
            name=self.name, method=self.method, status="ok", results=len(hits)
        )
