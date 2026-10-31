# app/sources/google_play_scraper_adapter.py
"""Google Play adapter using the google-play-scraper library.

Unlike direct HTTP fetch (returns 0 results — JS-rendered page), this library
handles Play Store scraping internally with no Playwright or API key required.
Runs the blocking library calls in a thread pool to keep the event loop free.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial

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
from app.utils.http_client import call_with_backoff
from app.sources.base import SourceAdapter, country_label, scan_countries

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="gplay")


def _search(term: str, country: str, n_hits: int = 30) -> list[dict]:
    """google_play_scraper.search() with its parsing bug fixed.

    The library reads the "top result" with dataset["ds:4"][0][1][0][23][16] and only catches
    IndexError, but for many queries (typically misspellings) that path is None and it raises
    TypeError ("'NoneType' object is not subscriptable") even though a normal result list exists.
    Here a missing top result just means "no pinned top result", and the first result list found
    is used.
    """
    import json
    from urllib.parse import quote

    from google_play_scraper.constants.element import ElementSpecs
    from google_play_scraper.constants.regex import Regex
    from google_play_scraper.constants.request import Formats
    from google_play_scraper.exceptions import NotFoundError
    from google_play_scraper.utils.request import get

    query = quote(term)
    try:
        dom = get(Formats.Searchresults.build(query=query, lang="en", country=country))
    except NotFoundError:
        dom = get(Formats.Searchresults.fallback_build(query=query, lang="en"))

    dataset: dict = {}
    for block in Regex.SCRIPT.findall(dom):
        key, value = Regex.KEY.findall(block), Regex.VALUE.findall(block)
        if key and value:
            dataset[key[0]] = json.loads(value[0])

    sections = dataset["ds:4"][0][1]
    try:
        top_result = sections[0][23][16]
    except (IndexError, TypeError):
        top_result = None

    apps = None
    for section in sections:  # the list sits at a different index for different countries/queries
        try:
            apps = section[22][0]
            break
        except (IndexError, TypeError):
            continue
    if apps is None:
        return []

    results = (
        [{k: spec.extract_content(top_result) for k, spec in ElementSpecs.SearchResultOnTop.items()}]
        if top_result else []
    )
    for app in apps[: max(0, n_hits - len(results))]:
        results.append({k: spec.extract_content(app) for k, spec in ElementSpecs.SearchResult.items()})
    return results


class GooglePlayScraperAdapter(SourceAdapter):
    name = "google_play"
    method = SourceMethod.scrape
    host_group = "app_store"
    max_results = 30  # per search term (each hit is labelled with the term that found it)

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        terms = brand_terms(ctx, kinds=("original", "package"), limit=3, variants=2)

        if not terms:
            return [], SourceStatus(name=self.name, method=self.method, status="skipped",
                                    error="no brand-relevant keywords")

        countries = scan_countries(ctx)
        android_package = ctx.request.android_package

        errors: list[str] = []

        def _search_sync() -> list[dict]:
            try:
                import google_play_scraper  # noqa: F401
            except ImportError as e:
                log.error("google_play: google-play-scraper not installed: %s", e)
                errors.append("google-play-scraper not installed")
                return []
            raw: list[dict] = []
            seen_pkgs: set[str] = set()
            for term in terms:
                for country in countries:
                    try:
                        results = call_with_backoff(partial(_search, term, country.lower(), n_hits=30))
                        for r in results:
                            if not r:  # the library returns None for entries it can't parse
                                continue
                            pkg = r.get("appId", "")
                            if pkg and pkg not in seen_pkgs:
                                seen_pkgs.add(pkg)
                                raw.append({**r, "_term": country_label(term, country, countries)})
                    except Exception as e:
                        log.warning(
                            "google_play: search failed for term=%r country=%r: %s: %s",
                            term, country, type(e).__name__, e,
                        )
                        errors.append(f"search {term!r}/{country}: {type(e).__name__}: {e}")
                        continue
            # The library can fail to parse the brand's own listing in search results;
            # fetch it directly so the official app is always present for comparison.
            if android_package and android_package not in seen_pkgs:
                from google_play_scraper import app as gp_app
                # An app can be unavailable in some countries: try each requested one until it is found.
                failures = []
                for country in countries:
                    try:
                        info = call_with_backoff(partial(gp_app, android_package, country=country.lower()))
                    except Exception as e:
                        failures.append(f"{country}: {e}")
                        continue
                    # First, so the per-query result cap can never drop it.
                    raw.insert(0, {
                        "_term": android_package,
                        "appId": android_package, "title": info.get("title"),
                        "developer": info.get("developer"), "icon": info.get("icon"),
                        "version": info.get("version"),
                    })
                    break
                else:
                    msg = f"official app {android_package!r} lookup failed in every requested country ({'; '.join(failures)})"
                    log.warning("google_play: %s", msg)
                    errors.append(msg)
            return raw

        loop = asyncio.get_running_loop()
        try:
            raw_results = await loop.run_in_executor(_executor, _search_sync)
        except Exception as e:
            log.warning("google_play: executor error: %s", e)
            return [], SourceStatus(name=self.name, method=self.method,
                                    status="failed", error=f"executor error: {e}")

        hits: list[Hit] = []
        seen: set[str] = set()

        for r in raw_results:
            pkg = r.get("appId", "")
            if not pkg:
                continue
            url = f"https://play.google.com/store/apps/details?id={pkg}"
            uid = hashlib.sha256(url.encode()).hexdigest()[:32]
            if uid in seen:
                continue
            seen.add(uid)

            is_official = bool(android_package and pkg == android_package)

            hits.append(Hit(
                id=uid,
                type=HitType.app_listing,
                title=r.get("title"),
                app_name=r.get("title"),
                package_id=pkg,
                version=r.get("version") or None,
                publisher=r.get("developer"),
                icon_url=r.get("icon") or None,
                url=url,
                is_official_match=is_official,
                host=HostInfo(
                    group=self.host_group,
                    name="Google Play",
                    domain="play.google.com",
                    is_official_store=True,
                ),
                # The term that actually found it, so the per-query result cap applies per search,
                # not to every Play result combined.
                found_via=[FoundVia(source=self.name, method=self.method,
                                    query=r.get("_term") or (terms[0] if terms else ""))],
                relevance_score=0,
                discovered_at=datetime.datetime.now(datetime.timezone.utc),
            ))

        # Partial failures are reported (and were logged above), not hidden behind a bare "ok".
        failed_all = bool(errors) and not hits
        return hits, SourceStatus(
            name=self.name, method=self.method, status="failed" if failed_all else "ok",
            results=len(hits), error="; ".join(errors)[:500] or None,
        )