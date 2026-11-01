
# app/services/orchestrator.py
"""
Scan orchestrator — runs all source adapters, deduplicates, scores, and
assembles the final ScanReport.

Called from inside the Dramatiq scan_actor. run_scan never raises: if the pipeline fails or hits
SCAN_TIMEOUT_SECONDS, the report is built from whatever was found so far and carries `error`.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time
from dataclasses import dataclass, field

import redis.asyncio as aioredis

from app.api.models import (
    Hit,
    ScanQuery,
    ScanContext,
    ScanReport,
    ScanRequest,
    ScanSummary,
    SourceStatus,
)
from app.utils.http_client import is_blocked_message
from app.config.settings import get_settings
from app.services.allowlist import (
    APP_STORE_DOMAIN,
    PLAY_DOMAIN,
    _norm_publisher,
    audit_item,
    drop_official,
    official_publishers,
    split_allowlisted,
)
from app.services.deduplicator import deduplicate, group_mirrors
from app.services.keyword_expansion import expand
from app.services.scoring import (
    apply_page_assessment,
    min_relevance_for,
    min_relevance,
    ruleset_version,
    score_hits,
)
from app.sources.registry import get_all_adapters
from app.storage import scan_log, status_store

log = logging.getLogger(__name__)


@dataclass
class _State:
    """What the pipeline has produced so far, so a failure or timeout can still be reported."""
    started_at: datetime.datetime
    request: ScanRequest
    hits: list[Hit] = field(default_factory=list)
    allowlisted: list = field(default_factory=list)
    suppressed: list = field(default_factory=list)
    statuses: list[SourceStatus] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    expanded_keywords: list = field(default_factory=list)


async def run_scan(scan_id: str, request: ScanRequest, redis_client: aioredis.Redis) -> ScanReport:
    """
    Execute a full scan for the given request. Never raises.

    On an unexpected error or when SCAN_TIMEOUT_SECONDS elapses, returns a report holding whatever was
    found up to that point (unscored if scoring had not finished), with `error` describing the failure.
    """
    settings = get_settings()
    t0 = time.monotonic()
    st = _State(started_at=datetime.datetime.now(datetime.timezone.utc), request=request)
    error: str | None = None
    try:
        await asyncio.wait_for(_pipeline(scan_id, request, redis_client, st), settings.scan_ceiling_seconds())
    except asyncio.TimeoutError:
        error = f"scan timed out after {settings.scan_ceiling_seconds()}s"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    if error:
        _note(scan_id, "ERROR", f"pipeline stopped: {error}")
        st.warnings.append(f"Scan stopped early ({error}); results are what was found so far.")
        if st.hits and not any(h.relevance_score for h in st.hits):
            st.warnings.append("Results were not scored.")
    await status_store.set_progress(redis_client, scan_id, 100)
    return _build_report(scan_id, st, error, int((time.monotonic() - t0) * 1000))


def _build_report(scan_id: str, st: _State, error: str | None, duration_ms: int) -> ScanReport:
    request = st.request
    hits = sorted(st.hits, key=lambda h: h.relevance_score, reverse=True)

    by_group: dict[str, int] = {}
    for hit in hits:
        group = hit.host.group if hit.host else "unknown"
        by_group[group] = by_group.get(group, 0) + 1

    summary = ScanSummary(
        total_results=len(hits),
        by_source_group=by_group,
        sources_total=len(st.statuses),
        sources_ok=sum(1 for s in st.statuses if s.status in ("ok", "partial")),
        sources_failed=sum(1 for s in st.statuses if s.status in ("failed", "blocked")),
        sources_skipped=sum(1 for s in st.statuses if s.status == "skipped"),
        allowlisted_count=len(st.allowlisted),
        suppressed_count=len(st.suppressed),
        duration_ms=duration_ms,
        scoring_ruleset=ruleset_version(),
    )
    return ScanReport(
        scan_id=scan_id,
        query=ScanQuery(
            **(request.submitted or request.model_dump(exclude={"submitted"})),
            received_at=st.started_at,
        ),
        summary=summary,
        results=hits,
        allowlisted=st.allowlisted,
        suppressed=st.suppressed,
        warnings=st.warnings,
        expanded_keywords=st.expanded_keywords,
        source_statuses=st.statuses,
        error=error,
    )


def _note(scan_id: str, level: str, message: str) -> None:
    """Record a problem both in the process log and in the scan's own scan.log."""
    getattr(log, "error" if level == "ERROR" else "warning" if level == "WARN" else "info")("[%s] %s", scan_id, message)
    try:
        scan_log.append_sync(get_settings().scan_log_path(scan_id), scan_id, level, message)
    except OSError as exc:
        log.error("[%s] could not write scan.log: %s", scan_id, exc)


def _official_listing_warnings(request: ScanRequest, hits: list[Hit], statuses: list[SourceStatus]) -> list[str]:
    """Warn when the caller's official android_package / ios_bundle cannot be found on its store.

    Without the official listing the scan cannot learn the official publisher, compare icons or
    permissions, so the brand's own apps (and sister apps by the same publisher) may be reported as rogue.
    """
    by_name = {s.name: s for s in statuses}
    android_stores = {"play.google.com", "global.app.mi.com", "appgallery.huawei.com"}
    checks = (
        ("android_package", request.android_package, android_stores,
         "any Android store searched (Google Play, GetApps, Huawei AppGallery)", "google_play"),
        ("ios_bundle", request.ios_bundle, {"apps.apple.com"}, "the App Store", "itunes"),
    )
    out = []
    for name, value, domains, store, source in checks:
        if not value:
            continue
        if any(h.host and h.host.domain in domains and (h.package_id or "").lower() == value.lower() for h in hits):
            continue
        src = by_name.get(source)
        if src and src.status in ("failed", "blocked", "partial"):
            why = f"the {source} source {src.status} ({src.error})"
        else:
            why = "it was looked up directly and is not listed"
        out.append(
            f"{name} '{value}' was not found on {store} ({why}). Official-app checks (icon and "
            f"permission comparison) are skipped, so the brand's own apps may be reported as rogue; "
            f"check the value and list the brand's other apps in allowlist_packages / official_publishers."
        )
    return out


def _learn_app_store_sellers(request: ScanRequest, hits: list[Hit]) -> ScanRequest:
    """Add the seller id (artistId) of the brand's own App Store listings to app_store_seller_ids.

    A bundle id is unique on the App Store, so the account that publishes a declared bundle there is the brand's.
    """
    sellers = list(request.app_store_seller_ids)
    for h in hits:
        if h.is_official_match and h.developer_id and h.host and h.host.domain == APP_STORE_DOMAIN:
            if h.developer_id not in sellers:
                sellers.append(h.developer_id)
    return request.model_copy(update={"app_store_seller_ids": sellers})


async def _stamp_play_developers(scan_id: str, request: ScanRequest, hits: list[Hit]) -> list[Hit]:
    """Set developer_id on the Google Play hits that show one of the brand's publisher names.

    Play search results carry the developer's name but not its id, and a name can be copied. Each such hit's
    listing is read for the id, so the allowlist can tell the brand's own account from a copy that uses its name.
    Hits with another publisher name are not looked up: an account shows one name on all its apps.
    """
    if not request.play_developer_ids:
        return hits
    publishers = official_publishers(request)
    todo = [
        i for i, h in enumerate(hits)
        if h.host and h.host.domain == PLAY_DOMAIN and h.package_id and not h.developer_id
        and _norm_publisher(h.publisher) in publishers
    ]
    if not todo:
        return hits
    from app.services.play_listing import fetch_account

    countries = get_settings().default_country_list()
    sem = asyncio.Semaphore(3)

    async def _one(pkg: str) -> str | None:
        async with sem:
            return (await fetch_account(pkg, countries))[1]

    ids = await asyncio.gather(*[_one(hits[i].package_id) for i in todo], return_exceptions=True)
    out = list(hits)
    for i, dev_id in zip(todo, ids):
        if isinstance(dev_id, BaseException):
            _note(scan_id, "WARN", f"Play developer lookup for {hits[i].package_id} failed: {dev_id}")
        elif dev_id:
            out[i] = hits[i].model_copy(update={"developer_id": dev_id})
    _note(scan_id, "INFO", f"read the Play developer id of {len(todo)} hit(s) that show a brand publisher name")
    return out


async def _pipeline(scan_id: str, request: ScanRequest, redis_client: aioredis.Redis, st: _State) -> None:
    settings = get_settings()

    def warn(message: str) -> None:
        st.warnings.append(message)
        _note(scan_id, "WARN", message)

    keywords = expand(request)
    st.expanded_keywords = keywords

    # Fetch official permissions for permission-similarity scoring (Play Store, no APK needed).
    official_permissions: list[str] = []
    if request.android_package:
        try:
            from app.services.play_listing import fetch_permissions
            official_permissions = await fetch_permissions(request.android_package)
            if not official_permissions:
                warn(f"No permissions could be read for android_package '{request.android_package}' from Google Play; "
                     "permission comparison is skipped.")
        except Exception as exc:
            warn(f"Official permission fetch failed: {exc}")
    # The official developer is whoever publishes the caller's own android_package on Google Play. Learning it
    # here means sister apps by the same company are recognised without the caller listing official_publishers,
    # and the developer account id allowlists every Play app that account publishes.
    if request.android_package:
        try:
            from app.services.play_listing import fetch_account
            developer, developer_id = await fetch_account(request.android_package, settings.default_country_list())
        except Exception as exc:
            developer = developer_id = None
            _note(scan_id, "WARN", f"official developer lookup failed: {exc}")
        if developer and developer.casefold() not in {p.casefold() for p in request.official_publishers}:
            request = request.model_copy(update={"official_publishers": [*request.official_publishers, developer]})
            _note(scan_id, "INFO", f"official publisher '{developer}' read from the Google Play listing of {request.android_package}")
        if developer_id and developer_id not in request.play_developer_ids:
            request = request.model_copy(update={"play_developer_ids": [*request.play_developer_ids, developer_id]})
            _note(scan_id, "INFO", f"official Play developer id '{developer_id}' read from the listing of {request.android_package}")

    ctx = ScanContext(
        scan_id=scan_id,
        request=request,
        expanded_keywords=keywords,
        official_permissions=official_permissions,
    )

    await status_store.update_meta_field(redis_client, scan_id, phase="collecting")
    await status_store.set_progress(redis_client, scan_id, 5)

    # Run all source adapters concurrently
    adapters = get_all_adapters()
    total = len(adapters)
    all_hits: list[Hit] = []
    source_statuses = st.statuses

    # Icon cache populated in parallel with source collection — scoring reuses it
    icon_cache: dict[str, str] = {}

    async def _prefetch_icons(hits: list[Hit]) -> None:
        try:
            from app.utils.phash import compute_phash
            await asyncio.gather(
                *[compute_phash(h.icon_url, cache=icon_cache) for h in hits if h.icon_url],
                return_exceptions=True,
            )
        except Exception as exc:
            _note(scan_id, "WARN", f"icon prefetch failed: {type(exc).__name__}: {exc}")

    # Per-scan concurrency limit on outbound requests
    sem = asyncio.Semaphore(8)

    async def _run_one(adapter: object) -> tuple[list[Hit], SourceStatus]:
        async with sem:
            return await adapter._run(ctx)

    tasks = [_run_one(adapter) for adapter in adapters]
    prefetch_tasks: list[asyncio.Task] = []
    completed = 0

    for coro in asyncio.as_completed(tasks):
        hits, status = await coro
        all_hits.extend(hits)
        st.hits = list(all_hits)  # raw hits are reportable if a later step fails
        if status.status == "failed" and is_blocked_message(status.error):
            # Refused (bot protection, 403, rate limit) is not the same as down: say so in the report
            status = status.model_copy(update={"status": "blocked"})
        elif status.status == "ok" and status.error:
            # Some queries answered and some failed: the source worked, but not completely
            status = status.model_copy(update={"status": "partial"})
        source_statuses.append(status)
        completed += 1
        if status.status in ("failed", "blocked", "partial"):
            _note(scan_id, "WARN", f"source {status.name}: {status.status} — {status.error}")
            warn(f"source {status.name} {status.status}: {status.error}")
        elif status.status == "skipped":
            _note(scan_id, "INFO", f"source {status.name}: skipped — {status.error}")
        if status.raw_results:
            _note(scan_id, "INFO", f"source {status.name}: kept {status.results} of {status.raw_results} hits (per-query cap)")

        # Start icon prefetch immediately for this batch — runs in background
        if hits:
            prefetch_tasks.append(asyncio.create_task(_prefetch_icons(hits)))

        await status_store.set_progress(redis_client, scan_id, 5 + int((completed / total) * 70))
        await status_store.update_sources(redis_client, scan_id, source_statuses)

    for message in _official_listing_warnings(request, all_hits, source_statuses):
        warn(message)

    await status_store.update_meta_field(redis_client, scan_id, phase="deduplicating")
    await status_store.set_progress(redis_client, scan_id, 80)

    deduped = deduplicate(all_hits)
    st.hits = deduped

    request = _learn_app_store_sellers(request, deduped)
    deduped = await _stamp_play_developers(scan_id, request, deduped)
    st.hits = deduped

    # Allowlist: drop the brand's own apps/domains/publishers before scoring.
    # Official listings stay until after scoring (icon comparison needs them).
    deduped, st.allowlisted = split_allowlisted(deduped, request)
    st.hits = deduped

    await status_store.update_meta_field(redis_client, scan_id, phase="scoring")
    await status_store.set_progress(redis_client, scan_id, 85)

    # Wait for any still-running icon prefetches before scoring uses the cache
    if prefetch_tasks:
        await asyncio.gather(*prefetch_tasks, return_exceptions=True)

    publishers = official_publishers(request)

    below_threshold: list[Hit] = []
    scored = await score_hits(
        deduped, request, official_permissions=ctx.official_permissions, icon_cache=icon_cache,
        official_publishers=publishers, dropped=below_threshold,
    )
    # Hits that scored under the reporting threshold are counted and audited, not silently lost
    st.suppressed.extend(
        audit_item(h, f"scored {h.relevance_score}, below the reporting threshold ({min_relevance_for(h)}): "
                      + (h.reasons[-1] if h.reasons else "weak match"))
        for h in below_threshold
    )
    st.hits = scored
    await status_store.set_progress(redis_client, scan_id, 95)

    scored, official_dropped = drop_official(scored)
    st.allowlisted = st.allowlisted + official_dropped

    # Hits that deeper inspection pushed under the reporting threshold are removed, with the reason kept
    def _suppress_low(hits: list[Hit]) -> list[Hit]:
        threshold = min_relevance()
        kept = [h for h in hits if h.relevance_score >= threshold]
        st.suppressed.extend(
            audit_item(h, h.reasons[-1] if h.reasons else "scored below the reporting threshold")
            for h in hits if h.relevance_score < threshold
        )
        return kept

    scored = _suppress_low(scored)
    scored, mirror_dupes = group_mirrors(scored)
    st.suppressed.extend(mirror_dupes)
    st.hits = scored

    # What each lookalike domain actually serves re-ranks it (brand bait up; parked, blank, neutral and unreachable pages drop out). The check was
    # made during discovery and kept on the hit, so it runs whether or not evidence files are saved.
    scored = [apply_page_assessment(h) for h in scored]
    scored = _suppress_low(scored)  # lookalike domains serving nothing suspicious now sit under the threshold
    scored.sort(key=lambda h: h.relevance_score, reverse=True)
    st.hits = scored

    # Evidence files are optional. When they are wanted, the riskiest hits get them first: lookalike domains and
    # hits that offer an APK, then score. Otherwise a popular brand's own pages fill the slots.
    if settings.capture_evidence and scored:
        await status_store.update_meta_field(redis_client, scan_id, phase="capturing evidence")
        from app.services.evidence import capture_evidence

        def _risk_order(h: Hit) -> tuple:
            lookalike = bool(h.host and h.host.group == "lookalike_domain")
            return (not lookalike, h.apk_url is None, -h.relevance_score)

        top = sorted((h for h in scored if not h.is_official_match), key=_risk_order)[: settings.evidence_max_hits]
        captured = await capture_evidence(
            scan_id, top, brand=request.brand_name, official_domain=request.official_domain
        )
        captured_by_id = {h.id: h for h in captured}
        scored = [captured_by_id.get(h.id, h) for h in scored]
        st.hits = scored