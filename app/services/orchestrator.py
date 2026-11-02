
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
    ScanContext,
    ScanQuery,
    ScanReport,
    ScanRequest,
    ScanSummary,
    SourceMethod,
    SourceStatus,
)
from app.config.settings import get_settings
from app.services.allowlist import (
    APP_STORE_DOMAIN,
    PLAY_DOMAIN,
    _norm_publisher,
    audit_item,
    publisher_matches,
    drop_official,
    official_publishers,
    split_allowlisted,
)
from app.services.deduplicator import deduplicate, group_mirrors
from app.services.keyword_expansion import expand
from app.services.scoring import (
    apply_page_assessment,
    min_relevance,
    min_relevance_for,
    ruleset_version,
    score_hit,
    score_hits,
)
from app.sources.registry import get_all_adapters
from app.storage import scan_log, status_store
from app.utils.browser import scan_browser
from app.utils.http_client import is_blocked_message

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
    # How far `hits` got: "collecting" (raw or deduplicated), "allowlisted" (unscored), "ranked" (the results)
    stage: str = "collecting"
    # The brand's own metadata per store, read from the store (see allowlist.store_metadata)
    store_meta: dict = field(default_factory=dict)
    # (name, method) of every source the scan started, so one that never finished is still reported
    sources: list[tuple[str, SourceMethod]] = field(default_factory=list)
    # (domain, listing field) of the brand's domains read from its Google Play listing for this scan
    derived_domains: list[tuple[str, str]] = field(default_factory=list)


async def run_scan(scan_id: str, request: ScanRequest, redis_client: aioredis.Redis) -> ScanReport:
    """
    Execute a full scan for the given request. Never raises.

    On an unexpected error or when SCAN_TIMEOUT_SECONDS elapses, returns a report holding whatever was
    found up to that point (unscored if scoring had not finished), with `error` describing the failure.
    """
    settings = get_settings()
    t0 = time.monotonic()
    st = _State(started_at=datetime.datetime.now(datetime.UTC), request=request)
    error: str | None = None
    try:
        # One Chromium for the whole scan: started by the first source that needs it, closed when the scan ends
        async with scan_browser():
            await asyncio.wait_for(_pipeline(scan_id, request, redis_client, st), settings.scan_ceiling_seconds())
    except TimeoutError:
        error = f"scan timed out after {settings.scan_ceiling_seconds()}s"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    if error:
        _note(scan_id, "ERROR", f"pipeline stopped: {error}")
        st.warnings.append(f"Scan stopped early ({error}); results are what was found so far.")
        done = {s.name for s in st.statuses}
        st.statuses.extend(
            SourceStatus(name=name, method=method, status="failed", error="did not finish before the scan stopped")
            for name, method in st.sources if name not in done
        )
        if st.stage != "ranked" and st.hits:
            try:
                await asyncio.wait_for(_rank_unfinished(scan_id, st), _SALVAGE_SECONDS)
                st.warnings.append(
                    "Results were allowlisted and scored from what was collected, without the icon check."
                )
            except Exception as exc:  # the raw hits are still reported, and marked as such
                _note(scan_id, "ERROR", f"could not score the collected hits: {type(exc).__name__}: {exc}")
        if st.hits and not any(h.relevance_score for h in st.hits):
            st.warnings.append("Results were not scored.")
    await status_store.set_progress(redis_client, scan_id, 100)
    return _build_report(scan_id, st, error, int((time.monotonic() - t0) * 1000))


# Time a scan that stopped early gets to rank what it collected, and how much of it the Play developer-id lookups
# may use (the rest of the work needs no network)
_SALVAGE_SECONDS = 90
_SALVAGE_LOOKUP_SECONDS = 60


def _suppress_low(st: _State, hits: list[Hit]) -> list[Hit]:
    """Hits that deeper inspection pushed under the reporting threshold are removed, with the reason kept."""
    threshold = min_relevance()
    st.suppressed.extend(
        audit_item(h, h.reasons[-1] if h.reasons else "scored below the reporting threshold")
        for h in hits if h.relevance_score < threshold
    )
    return [h for h in hits if h.relevance_score >= threshold]


def _rank(st: _State, scored: list[Hit], below_threshold: list[Hit]) -> None:
    """From scored hits to the report's results, in st.hits.

    Audits what scored under the threshold, drops the official listings, folds mirror copies of one package
    into one finding and re-ranks lookalike domains by what their page serves.
    """
    # Hits that scored under the reporting threshold are counted and audited, not silently lost
    st.suppressed.extend(
        audit_item(h, f"scored {h.relevance_score}, below the reporting threshold ({min_relevance_for(h)}): "
                      + (h.reasons[-1] if h.reasons else "weak match"))
        for h in below_threshold
    )
    scored, official_dropped = drop_official(scored)
    st.allowlisted = st.allowlisted + official_dropped

    scored = _suppress_low(st, scored)
    scored, mirror_dupes = group_mirrors(scored)
    st.suppressed.extend(mirror_dupes)

    # What each lookalike domain actually serves re-ranks it (brand bait up; parked, blank, neutral and unreachable pages drop out). The check was
    # made during discovery and kept on the hit, so it runs whether or not evidence files are saved.
    scored = [apply_page_assessment(h) for h in scored]
    scored = _suppress_low(st, scored)  # lookalike domains serving nothing suspicious now sit under the threshold
    scored.sort(key=lambda h: h.relevance_score, reverse=True)
    st.hits = scored
    st.stage = "ranked"


async def _rank_unfinished(scan_id: str, st: _State) -> None:
    """The pipeline stopped before its results were ranked: allowlist, score and rank what was collected.

    Otherwise the report would list every raw hit, the brand's own apps included, as a result with score 0.
    The Play developer-id lookups run within _SALVAGE_LOOKUP_SECONDS; the icon check is skipped.
    """
    request, hits = st.request, st.hits
    if st.stage == "collecting":
        hits = deduplicate(hits)
        hits = await _stamp_play_developers(scan_id, request, hits, timeout=_SALVAGE_LOOKUP_SECONDS)
        request = await _verify_off_play_packages(scan_id, request, hits, timeout=_SALVAGE_LOOKUP_SECONDS)
        st.request = request
        hits, st.allowlisted = split_allowlisted(hits, request, store_meta=st.store_meta)
        hits, official_dropped = drop_official(hits)
        st.allowlisted = st.allowlisted + official_dropped
    publishers = official_publishers(request)
    scored = list(await asyncio.gather(*[score_hit(h, request, publishers) for h in hits]))
    below = [h for h in scored if h.relevance_score < min_relevance_for(h)]
    kept = sorted((h for h in scored if h.relevance_score >= min_relevance_for(h)),
                  key=lambda h: h.relevance_score, reverse=True)
    _rank(st, kept, below)


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
        derived_official_domains=[{"domain": d, "from": f} for d, f in st.derived_domains],
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
    so the brand's own apps (and sister apps by the same publisher) may be reported as rogue.
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
            f"{name} '{value}' was not found on {store} ({why}). Official-app checks (icon "
            f"comparison) are skipped, so the brand's own apps may be reported as rogue; "
            f"check the value and the brand's store links."
        )
    return out


_MAX_OFF_PLAY_LOOKUPS = 20


async def _verify_off_play_packages(
    scan_id: str, request: ScanRequest, hits: list[Hit], timeout: float | None = None,
) -> ScanRequest:
    """The brand's apps found only on other stores, confirmed on Google Play.

    A listing on an APK site or another store that shows one of the brand's publisher names may be a sibling app
    the Play search did not return, or a copy that borrows the name. Its package is looked up on Google Play: when
    Play shows it under one of the brand's own developer accounts, it is the brand's app, and its id joins the
    official ids so the allowlist clears it (still only with the publisher match). A name alone never clears it.
    """
    if not request.play_developer_ids:
        return request
    publishers = official_publishers(request)
    own = request.official_app_ids()["android"]
    packages = list(dict.fromkeys(
        h.package_id for h in hits
        if h.package_id and h.host and h.host.domain not in (PLAY_DOMAIN, APP_STORE_DOMAIN)
        and h.package_id.strip().lower() not in own and publisher_matches(h.publisher, publishers)
    ))[:_MAX_OFF_PLAY_LOOKUPS]
    if not packages:
        return request
    from app.services.play_listing import fetch_account

    countries = get_settings().default_country_list()
    accounts = set(request.play_developer_ids)
    sem = asyncio.Semaphore(3)

    async def _one(pkg: str) -> str | None:
        async with sem:
            return (await fetch_account(pkg, countries))[1]

    lookups = {asyncio.create_task(_one(p)): p for p in packages}
    done, pending = await asyncio.wait(lookups, timeout=timeout)
    for task in pending:
        task.cancel()
    verified = [lookups[t] for t in done if t.exception() is None and t.result() in accounts]
    _note(scan_id, "INFO", f"{len(verified)} of {len(packages)} app(s) found on other stores under a brand publisher "
                           f"name are published by the brand's Google Play account: {', '.join(verified) or 'none'}")
    if not verified:
        return request
    return request.model_copy(update={"android_packages": list(dict.fromkeys([*request.android_packages, *verified]))})


async def _stamp_play_developers(
    scan_id: str, request: ScanRequest, hits: list[Hit], timeout: float | None = None,
) -> list[Hit]:
    """Set developer_id on the Google Play hits that show one of the brand's publisher names.

    Play search results carry the developer's name but not its id, and a name can be copied. Each such hit's
    listing is read for the id, so the allowlist can tell the brand's own account from a copy that uses its name.
    Hits with another publisher name are not looked up: an account shows one name on all its apps.
    With a timeout, the lookups still running then are dropped and the ids read so far are kept.
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
            developer, developer_id = await fetch_account(pkg, countries)
        if developer_id:
            _note(scan_id, "INFO", f"developer id lookup {pkg}: developer '{developer}' id '{developer_id}'")
        else:
            _note(scan_id, "WARN", f"developer id lookup {pkg}: no developer id (listing not read or none shown)")
        return developer_id

    lookups = {asyncio.create_task(_one(hits[i].package_id)): i for i in todo}
    done, pending = await asyncio.wait(lookups, timeout=timeout)
    for task in pending:
        task.cancel()
        _note(scan_id, "WARN", f"developer id lookup {hits[lookups[task]].package_id}: timed out, not read")
    out = list(hits)
    for task in done:
        i = lookups[task]
        if task.exception() is not None:
            _note(scan_id, "WARN", f"Play developer lookup for {hits[i].package_id} failed: {task.exception()}")
        elif task.result():
            out[i] = hits[i].model_copy(update={"developer_id": task.result()})
    _note(scan_id, "INFO", f"read the Play developer id of {len(done)} of {len(todo)} hit(s) that show a brand publisher name")
    return out


async def _pipeline(scan_id: str, request: ScanRequest, redis_client: aioredis.Redis, st: _State) -> None:
    settings = get_settings()

    def warn(message: str) -> None:
        st.warnings.append(message)
        _note(scan_id, "WARN", message)

    keywords = expand(request)
    st.expanded_keywords = keywords

    if request.is_name_only():
        warn("Name-only scan: the brand has no stored apps, domains or publisher, so nothing can be compared with "
             "its own listings. Only apps named exactly like the brand or pairing it with scam, mod or coupon "
             "wording are reported, and the brand's own apps may be among them. Store the brand with "
             "POST /v1/brand-details for a full scan.")

    # The official developer is whoever publishes the caller's own android_package on Google Play. Learning it
    # here means sister apps by the same company are recognised without the caller listing official_publishers,
    # and the developer account id allowlists every Play app that account publishes.
    from app.services.play_listing import fetch_account
    play_listings: list[tuple[str, str | None, str | None]] = []  # (package, developer name, developer id)
    for pkg in request.android_packages:
        try:
            developer, developer_id = await fetch_account(pkg, settings.default_country_list())
        except Exception as exc:
            developer = developer_id = None
            _note(scan_id, "WARN", f"official developer lookup failed for {pkg}: {exc}")
        play_listings.append((pkg, developer, developer_id))
        if developer and developer.casefold() not in {p.casefold() for p in request.official_publishers}:
            request = request.model_copy(update={"official_publishers": [*request.official_publishers, developer]})
            _note(scan_id, "INFO", f"official publisher '{developer}' read from the Google Play listing of {pkg}")
        if developer_id and developer_id not in request.play_developer_ids:
            request = request.model_copy(update={"play_developer_ids": [*request.play_developer_ids, developer_id]})
            _note(scan_id, "INFO", f"official Play developer id '{developer_id}' read from the listing of {pkg}")
    st.request = request  # with what was learned, so a scan that stops early still allowlists with it

    # The brand's own Play listing gives its icon (the reference for icon comparison; the listing itself is
    # allowlisted, so it is not among the hits) and the domains the developer publishes. Those domains are used for
    # this scan only; the brand record is not changed.
    official_icon_url: str | None = None
    if request.android_package:
        try:
            from app.services.play_listing import fetch_listing, listing_domains, listing_icon
            listing = await fetch_listing(request.android_package, settings.default_country_list())
            official_icon_url = listing_icon(listing)
            st.derived_domains = listing_domains(listing)
        except Exception as exc:
            warn(f"Official listing read failed: {exc}")
    from app.sources.itunes import app_store_listing
    app_store_listings: list[tuple[str, str | None, str | None]] = []  # (bundle, seller name, artist id)
    for bundle in request.ios_bundles:
        try:
            listing_ios = await app_store_listing(bundle, settings.default_country_list())
        except Exception as exc:
            listing_ios = None
            warn(f"Official App Store listing read failed for {bundle}: {exc}")
        if listing_ios:
            app_store_listings.append((bundle, listing_ios["publisher"], listing_ios["seller_id"]))
            if official_icon_url is None:  # no Play icon: the App Store icon is the reference instead
                official_icon_url = listing_ios["icon"]
    # Each store's own listings are what allowlisting compares that store's hits against
    from app.services.allowlist import store_metadata
    st.store_meta = store_metadata(play_listings, app_store_listings)
    derived =[domain for domain, _ in st.derived_domains if domain not in request.official_domains]
    if derived:
        request = request.model_copy(update={
            "official_domains": [*request.official_domains, *derived],
            "official_domain": request.official_domain or derived[0],
        })
        _note(scan_id, "INFO", f"official domains read from the Google Play listing: {', '.join(derived)}")
    st.request = request

    ctx = ScanContext(
        scan_id=scan_id,
        request=request,
        expanded_keywords=keywords,
        official_icon_url=official_icon_url,
    )

    await status_store.update_meta_field(redis_client, scan_id, phase="collecting")
    await status_store.set_progress(redis_client, scan_id, 5)

    # Run all source adapters concurrently
    adapters = get_all_adapters()
    st.sources = [(a.name, a.method) for a in adapters]
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

    tasks = [asyncio.create_task(_run_one(adapter)) for adapter in adapters]
    prefetch_tasks: list[asyncio.Task] = []
    completed = 0

    try:
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
    finally:
        # A scan stopped by its time limit cancels the sources still running instead of leaving them behind
        for task in tasks:
            task.cancel()

    for message in _official_listing_warnings(request, all_hits, source_statuses):
        warn(message)

    await status_store.update_meta_field(redis_client, scan_id, phase="deduplicating")
    await status_store.set_progress(redis_client, scan_id, 80)

    deduped = deduplicate(all_hits)
    st.hits = deduped

    st.request = request
    deduped = await _stamp_play_developers(scan_id, request, deduped)
    st.hits = deduped
    request = await _verify_off_play_packages(scan_id, request, deduped)
    st.request = request

    # Allowlist: drop the brand's own apps/domains/publishers before scoring and before the threshold.
    deduped, st.allowlisted = split_allowlisted(deduped, request, store_meta=st.store_meta)
    # The brand's official listings are allowlisted too. Their icon is kept as the reference for the icon check.
    official_icon = next((h.icon_url for h in deduped if h.is_official_match and h.icon_url), None)
    deduped, official_dropped = drop_official(deduped)
    st.allowlisted = st.allowlisted + official_dropped
    st.hits = deduped
    st.stage = "allowlisted"

    await status_store.update_meta_field(redis_client, scan_id, phase="scoring")
    await status_store.set_progress(redis_client, scan_id, 85)

    # Wait for any still-running icon prefetches before scoring uses the cache
    if prefetch_tasks:
        await asyncio.gather(*prefetch_tasks, return_exceptions=True)

    publishers = official_publishers(request)

    below_threshold: list[Hit] = []
    scored = await score_hits(
        deduped, request, icon_cache=icon_cache,
        reference_icon_url=official_icon or ctx.official_icon_url,
        official_publishers=publishers, dropped=below_threshold,
    )
    await status_store.set_progress(redis_client, scan_id, 95)
    _rank(st, scored, below_threshold)
    scored = st.hits

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
