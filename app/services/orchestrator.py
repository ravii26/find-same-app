
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
    ApkAnalysisResult,
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
    audit_item,
    drop_official,
    official_publishers,
    split_allowlisted,
    split_cert_verified,
)
from app.services.deduplicator import deduplicate, group_mirrors
from app.services.keyword_expansion import expand
from app.services.brand_match import presence_any
from app.services.scoring import (
    apply_apk_brand_check,
    apply_apk_permissions,
    apply_cert_mismatch,
    apply_page_assessment,
    min_relevance_for,
    apply_unverified_official_copy,
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


def merge_apk_analysis(existing: ApkAnalysisResult | None, result: ApkAnalysisResult) -> ApkAnalysisResult:
    """Combine what the store exposed (e.g. Aptoide's SHA-1) with what the downloaded APK showed.

    Every value the analysis produced wins, certificate SHA-256 included: dropping it left Aptoide hits with
    only the store's SHA-1 and no way to compare against `official_cert_sha256`.
    """
    if existing is None:
        return result
    return existing.model_copy(update={k: v for k, v in result.model_dump().items() if v is not None})


_DISCOVERY_GROUPS = ("web_search", "lookalike_domain")


async def _bounded_gather(tasks: list[asyncio.Task], timeout: float) -> set[asyncio.Task]:
    """Run tasks within a time budget; cancel and drain anything that didn't finish."""
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return done


async def discover_apks(hits: list[Hit], request: ScanRequest, limit: int, budget: float) -> tuple[list[Hit], int]:
    """Open web pages that other sources found (search results, lookalike domains) and attach the APK
    each one offers as `apk_url`, so the APK analysis can check it. Returns (hits, number of pages opened).

    Lookalike-domain hits already went through the same check in their adapter and are skipped. Pages are
    opened with bounded concurrency inside a time budget; whatever finished is kept.
    """
    from app.sources.lookalike_domain_adapter import apk_for_page

    targets = [
        h for h in hits
        if h.type.value == "web_page" and not h.apk_url and h.host and h.host.group in _DISCOVERY_GROUPS
        and not any(f.source == "lookalike_domains" for f in h.found_via)
    ][:limit]
    if not targets:
        return hits, 0

    sem = asyncio.Semaphore(6)

    async def _one(hit: Hit) -> tuple[str, str | None]:
        async with sem:
            return hit.id, await apk_for_page(hit.url, request.brand_name, request.official_domain)

    tasks = [asyncio.create_task(_one(h)) for h in targets]
    done = await _bounded_gather(tasks, budget)
    found = {hid: apk for hid, apk in (t.result() for t in done if not t.exception()) if apk}
    updated = [h.model_copy(update={"apk_url": found[h.id]}) if h.id in found else h for h in hits]
    return updated, len(done)


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
            analyze_apks=get_settings().analyze_apks,
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
    for field, value, domains, store, source in checks:
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
            f"{field} '{value}' was not found on {store} ({why}). Official-app checks (icon and "
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
            from app.analysis.apk_similarity import fetch_permissions
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
            from app.analysis.apk_similarity import fetch_developer
            developer = await fetch_developer(request.android_package)
        except Exception as exc:
            developer = None
            _note(scan_id, "WARN", f"official developer lookup failed: {exc}")
        if developer and developer.casefold() not in {p.casefold() for p in request.official_publishers}:
            request = request.model_copy(update={"official_publishers": [*request.official_publishers, developer]})
            _note(scan_id, "INFO", f"official publisher '{developer}' read from the Google Play listing of {request.android_package}")

    official_apk_permissions: list[str] = []

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

    # Allowlist: drop the brand's own apps/domains/publishers before scoring.
    # Official listings stay until after scoring (icon comparison needs them).
    deduped, st.allowlisted = split_allowlisted(deduped, request)
    st.hits = deduped

    if settings.analyze_apks:
        # Fake bank/payment apps mostly spread through a web page, not a store: look for the file they offer
        await status_store.update_meta_field(redis_client, scan_id, phase="looking for offered apks")
        deduped, opened = await discover_apks(
            deduped, request, settings.apk_discovery_pages, settings.apk_discovery_seconds,
        )
        st.hits = deduped
        offering = sum(1 for h in deduped if h.apk_url and h.type.value == "web_page")
        _note(scan_id, "INFO", f"apk discovery: opened {opened} page(s), {offering} offer an APK")

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

    # APK static analysis pass — download and analyze up to 10 non-official hits with APK URLs
    if settings.analyze_apks:
        await status_store.update_meta_field(redis_client, scan_id, phase="analyzing apks")
        from app.analysis.apk_analyzer import analyze_from_url
        official_cert = None

        # Only hits with real brand evidence are worth a download: exact matches first, then close
        # lookalike spellings. A name that is merely loosely similar ("Phonemes") is not analyzed.
        def _evidence(h: Hit) -> str | None:
            return presence_any([request.brand_name, *request.brand_aliases], h.app_name or h.title, h.package_id, h.publisher)

        candidates = [h for h in scored if h.apk_url and not h.is_official_match and _evidence(h) in ("exact", "fuzzy")]
        candidates.sort(key=lambda h: (_evidence(h) != "exact", -h.relevance_score))
        to_analyze = candidates[:10]
        if to_analyze:
            apk_sem = asyncio.Semaphore(3)

            async def _analyze_one(hit: Hit) -> Hit:
                async with apk_sem:
                    try:
                        result = await analyze_from_url(hit.apk_url, official_cert)  # type: ignore[arg-type]
                        return hit.model_copy(update={"apk_analysis": merge_apk_analysis(hit.apk_analysis, result)})
                    except Exception as exc:
                        warn(f"APK analysis failed for {hit.url}: {exc}")
                        return hit

            # APK downloads are slow and unpredictable: give the whole pass its own budget so it cannot eat the
            # scan's time limit, and keep the APKs that finished.
            tasks = [asyncio.create_task(_analyze_one(h)) for h in to_analyze]
            done = await _bounded_gather(tasks, settings.apk_phase_seconds)
            if len(done) < len(tasks):
                warn(
                    f"APK analysis stopped after {settings.apk_phase_seconds}s: {len(done)} of {len(tasks)} APKs were "
                    "analysed, the rest are reported without APK evidence."
                )
            analyzed = [t.result() if t in done else h for t, h in zip(tasks, to_analyze)]
            analyzed_by_id = {h.id: h for h in analyzed}
            scored = [analyzed_by_id.get(h.id, h) for h in scored]
            # Analysis may have produced a definitive cert_match the store didn't expose
            scored = [apply_cert_mismatch(h, request) for h in scored]
            scored = [apply_apk_permissions(h, official_apk_permissions, request) for h in scored]
            # Last: an APK that never references the brand is demoted (and then suppressed below)
            scored = [apply_apk_brand_check(h, request) for h in scored]
            scored.sort(key=lambda h: h.relevance_score, reverse=True)
            st.hits = scored

    # Copies of the official package whose certificate was never checked must not outrank real impersonators
    scored = [apply_unverified_official_copy(h, request) for h in scored]
    scored.sort(key=lambda h: h.relevance_score, reverse=True)

    scored, official_dropped = drop_official(scored)
    st.allowlisted = st.allowlisted + official_dropped
    scored, verified_mirrors = split_cert_verified(scored, request)
    st.allowlisted = st.allowlisted + verified_mirrors

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