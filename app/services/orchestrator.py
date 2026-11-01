
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
    SourceMethod,
    SourceStatus,
)
from app.utils.http_client import is_blocked_message
from app.config.settings import get_settings
from app.services.allowlist import (
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
    # Sources the scan started, so a stopped scan can report the ones that never finished
    expected_sources: list[tuple[str, SourceMethod]] = field(default_factory=list)
    # True once `hits` hold scored, threshold-checked results
    scored: bool = False


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
        _mark_unfinished(st, f"the scan stopped early: {error}")
        if st.hits and not st.scored:
            try:
                await asyncio.wait_for(_salvage(st), _SALVAGE_SECONDS)
            except Exception as exc:  # the report must still be written
                _note(scan_id, "ERROR", f"scoring after the early stop failed: {type(exc).__name__}: {exc}")
            if not st.scored:
                st.warnings.append("Results were not scored.")
    await status_store.set_progress(redis_client, scan_id, 100)
    return _build_report(scan_id, st, error, int((time.monotonic() - t0) * 1000))


_SALVAGE_SECONDS = 60


def _mark_unfinished(st: _State, why: str) -> None:
    """Add a failed status for every started source that never reported, so the report lists all of them."""
    reported = {s.name for s in st.statuses}
    for name, method in st.expected_sources:
        if name not in reported:
            st.statuses.append(SourceStatus(
                name=name, method=method, status="failed", results=0,
                error=f"did not finish ({why}); its results are missing",
            ))


async def _salvage(st: _State) -> None:
    """Score what an early-stopped scan found, so the report still ranks it and applies the allowlist.

    Uses the name, package, domain and publisher signals only: the icon and permission checks need more network
    calls, which a stopped scan has no time for.
    """
    hits = deduplicate(st.hits)  # a no-op when the pipeline had already deduplicated
    hits, allowlisted = split_allowlisted(hits, st.request)
    st.allowlisted = st.allowlisted + allowlisted
    below: list[Hit] = []
    scored = await score_hits(
        hits, st.request, official_publishers=official_publishers(st.request), dropped=below, enrich=False,
    )
    _finish_scored(st, scored, below)
    st.warnings.append("Results were scored without the icon and permission checks, because the scan stopped early.")


def _finish_scored(st: _State, scored: list[Hit], below_threshold: list[Hit]) -> list[Hit]:
    """From scored hits to the final results: audit the low ones, drop official listings, merge mirrors."""
    # Hits that scored under the reporting threshold are counted and audited, not silently lost
    st.suppressed.extend(
        audit_item(h, f"scored {h.relevance_score}, below the reporting threshold ({min_relevance_for(h)}): "
                      + (h.reasons[-1] if h.reasons else "weak match"))
        for h in below_threshold
    )
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

    # What each lookalike domain actually serves re-ranks it (brand bait up; parked, blank, neutral and unreachable
    # pages drop out). The check was made during discovery and kept on the hit, so it runs whether or not evidence
    # files are saved.
    scored = [apply_page_assessment(h) for h in scored]
    scored = _suppress_low(scored)  # lookalike domains serving nothing suspicious now sit under the threshold
    scored.sort(key=lambda h: h.relevance_score, reverse=True)
    st.hits = scored
    st.scored = True
    return scored


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
    for id_field, value, domains, store, source in checks:
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
            f"{id_field} '{value}' was not found on {store} ({why}). Official-app checks (icon and "
            f"permission comparison) are skipped, so the brand's own apps may be reported as rogue; "
            f"check the value and list the brand's other apps in allowlist_packages / official_publishers."
        )
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
    # here means sister apps by the same company are recognised without the caller listing official_publishers.
    if request.android_package:
        try:
            from app.services.play_listing import fetch_developer
            developer = await fetch_developer(request.android_package)
        except Exception as exc:
            developer = None
            _note(scan_id, "WARN", f"official developer lookup failed: {exc}")
        if developer and developer.casefold() not in {p.casefold() for p in request.official_publishers}:
            request = request.model_copy(update={"official_publishers": [*request.official_publishers, developer]})
            st.request = request
            _note(scan_id, "INFO", f"official publisher '{developer}' read from the Google Play listing of {request.android_package}")

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

    source_limit = settings.source_timeout_seconds

    async def _run_one(adapter: object) -> tuple[list[Hit], SourceStatus]:
        async with sem:
            started = time.monotonic()
            try:
                return await asyncio.wait_for(adapter._run(ctx), source_limit)
            except asyncio.TimeoutError:
                return [], SourceStatus(
                    name=adapter.name, method=adapter.method, status="failed", results=0,
                    error=f"source timed out after {source_limit}s; its results are missing",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )

    st.expected_sources = [(a.name, a.method) for a in adapters]
    prefetch_tasks: list[asyncio.Task] = []
    completed = 0

    async def _collect(hits: list[Hit], status: SourceStatus) -> None:
        nonlocal completed
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

    # Collection has its own deadline, before the scan's: whatever a slow source has not returned by then is
    # reported as unfinished, and the time left goes to scoring and the report.
    loop = asyncio.get_running_loop()
    collect_deadline = loop.time() + settings.collection_seconds()
    pending = {asyncio.create_task(_run_one(adapter)) for adapter in adapters}
    try:
        while pending:
            remaining = collect_deadline - loop.time()
            if remaining <= 0:
                break
            done, pending = await asyncio.wait(pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await _collect(*task.result())
    finally:
        for task in pending:  # also when the scan itself is stopped: no source keeps running in the background
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    if pending:
        unfinished = len(pending)
        _mark_unfinished(st, f"collection stopped after {settings.collection_seconds()}s")
        warn(f"{unfinished} source(s) did not finish within {settings.collection_seconds()}s and were stopped; "
             "their results are missing (see source_statuses)")
        await status_store.update_sources(redis_client, scan_id, source_statuses)

    for message in _official_listing_warnings(request, all_hits, source_statuses):
        warn(message)

    await status_store.update_meta_field(redis_client, scan_id, phase="deduplicating")
    await status_store.set_progress(redis_client, scan_id, 80)

    deduped = deduplicate(all_hits)
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
    await status_store.set_progress(redis_client, scan_id, 95)
    scored = _finish_scored(st, scored, below_threshold)

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