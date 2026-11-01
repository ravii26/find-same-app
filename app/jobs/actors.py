# app/jobs/actors.py
"""
Dramatiq actors for scan execution and webhook delivery.

Two-queue model:
- Queue 1 (pending): Redis list of scans waiting for a slot (status_store).
- Queue 2 (running): Redis set capped at MAX_CONCURRENT_SCANS. Only scans in it are handed to Dramatiq
  (queue "scans"), so a message in Dramatiq always has a slot and is never retried or dropped for lack of one.
- When a scan finishes it leaves queue 2 and `promote_pending()` moves the next pending scan in.
"""

from __future__ import annotations

import asyncio
import datetime
import logging

import dramatiq
import redis.asyncio as aioredis

from app.api.models import ScanRequest, ScanStatus
from app.config.settings import get_settings
from app.jobs.broker import get_broker
from app.services.orchestrator import run_scan
from app.storage import result_store, scan_log, status_store
from app.utils.http_client import close_client, close_rate_limiter

log = logging.getLogger(__name__)

# Ensure broker is initialized
get_broker()

_HEARTBEAT_SECONDS = 30


def _make_redis() -> aioredis.Redis:
    """Create a fresh Redis client bound to the current event loop.

    Each scan runs inside asyncio.run() which creates and then closes its own
    event loop. aioredis connections are loop-bound, so a client cannot be
    shared across scans — each scan must own its own client and close it before
    asyncio.run() exits.
    """
    return aioredis.Redis.from_url(
        get_settings().redis_url, max_connections=5, decode_responses=False
    )


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


async def promote_pending(redis: aioredis.Redis) -> None:
    """Fill free slots of the running queue from the pending queue and hand those scans to the worker."""
    for scan_id in await status_store.reap_lost(redis):
        scan_log.append_sync(get_settings().scan_log_path(scan_id), scan_id, "ERROR", "worker lost; scan abandoned")
    for scan_id in await status_store.promote(redis):
        request_dict = await status_store.take_request(redis, scan_id)
        if request_dict is None:  # deleted while pending
            await status_store.remove_from_running(redis, scan_id)
            continue
        scan_actor.send(scan_id, request_dict)


@dramatiq.actor(queue_name="scans", max_retries=0, time_limit=get_settings().scan_ceiling_seconds() * 1000 + 120_000)
def scan_actor(scan_id: str, request_dict: dict) -> None:
    """Run one scan that already owns a slot in the running queue."""
    # Every log record from this scan (sources, probes, HTTP, rate limiter) is also written to its scan.log.
    with scan_log.capture(scan_id, get_settings().scan_log_path(scan_id)):
        asyncio.run(_execute_scan(scan_id, request_dict))


async def _heartbeat(redis: aioredis.Redis, scan_id: str) -> None:
    while True:
        # One failed write must not end the heartbeat: a dead task stops refreshing the alive key, and the reaper
        # then marks a scan that is still running as lost.
        try:
            await status_store.touch_alive(redis, scan_id)
        except Exception as exc:
            log.warning("heartbeat for %s failed: %s", scan_id, exc)
        await asyncio.sleep(_HEARTBEAT_SECONDS)


def _final_status(report) -> ScanStatus:
    if report.error:
        return ScanStatus.failed
    if report.warnings or report.summary.sources_failed > 0:
        return ScanStatus.completed_with_errors
    return ScanStatus.completed


def _indexing_problem(indexed: dict | None) -> str | None:
    """Warning text when mirroring the report into Elasticsearch did not fully work, else None."""
    if indexed is None:
        return "Elasticsearch indexing failed; the results are in the scan report only, not searchable in Elasticsearch."
    if indexed.get("failed"):
        return (
            f"Elasticsearch rejected {indexed['failed']} document(s) (indexed {indexed.get('indexed', 0)}); "
            "those results are in the scan report only."
        )
    return None





async def _execute_scan(scan_id: str, request_dict: dict) -> None:
    settings = get_settings()
    redis = _make_redis()
    log_path = settings.scan_log_path(scan_id)
    beat = asyncio.create_task(_heartbeat(redis, scan_id))
    try:
        if await status_store.get_status(redis, scan_id) is None:  # deleted after it was handed to the worker
            return
        scan_log.append_sync(log_path, scan_id, "INFO", "scan started")
        await status_store.set_status(redis, scan_id, ScanStatus.running)
        await status_store.update_meta_field(redis, scan_id, started_at=_now())

        request = ScanRequest.model_validate(request_dict)
        report = await run_scan(scan_id, request, redis)  # never raises; failures come back in report.error

        final = _final_status(report)
        report = report.model_copy(update={"status": final.value})
        result_store.write_result(report)
        await status_store.set_status(redis, scan_id, final)
        await status_store.update_meta_field(redis, scan_id, finished_at=_now())
        if report.error:
            scan_log.append_sync(log_path, scan_id, "ERROR", f"scan failed: {report.error}")
        scan_log.append_sync(
            log_path, scan_id, "INFO", f"scan finished — status={final.value} results={report.summary.total_results}"
        )

        from app.storage import es_store
        indexed = await es_store.index_report(report, request, final.value)  # always runs; never raises
        problem = _indexing_problem(indexed)
        if problem:
            # The report on disk is still complete, but whoever searches Elasticsearch must be told it is not
            scan_log.append_sync(log_path, scan_id, "WARN", problem)
            final = ScanStatus.completed_with_errors if final == ScanStatus.completed else final
            report = report.model_copy(update={"warnings": report.warnings + [problem], "status": final.value})
            result_store.write_result(report)
            await status_store.set_status(redis, scan_id, final)

    except Exception as exc:  # e.g. the report could not be written
        log.exception("Scan %s crashed: %s", scan_id, exc)
        scan_log.append_sync(log_path, scan_id, "ERROR", f"scan crashed: {exc}")
        try:
            await status_store.set_status(redis, scan_id, ScanStatus.failed)
            await status_store.update_meta_field(redis, scan_id, finished_at=_now())
        except Exception as inner:
            log.error("could not record failed status for %s: %s", scan_id, inner)
    finally:
        beat.cancel()
        # Close all async resources BEFORE asyncio.run() closes the loop.
        # Unclosed aioredis/httpx objects GC'd after loop close schedule callbacks on
        # the dead loop, causing "Event loop is closed" in the next scan on this thread.
        try:
            await close_client()
        except Exception:
            pass
        try:
            await close_rate_limiter()
        except Exception:
            pass
        try:
            await status_store.remove_from_running(redis, scan_id)
            await promote_pending(redis)  # a slot just opened: start the next pending scan
        except Exception as exc:
            log.error("could not promote next pending scan: %s", exc)
        try:
            await redis.aclose()
        except Exception:
            pass


