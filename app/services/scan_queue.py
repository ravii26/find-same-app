# app/services/scan_queue.py
"""Accepting scans and starting them: the queue rules, on top of the Redis state in status_store.

- submit(): accepts a request, or returns the scan an identical active request already has.
- promote_pending(): frees the slots of scans whose worker died, then starts pending scans while slots are free.
"""

from __future__ import annotations

import datetime

import redis.asyncio as aioredis
from ulid import ULID

from app.api.models import ScanRequest, ScanStatus
from app.config.settings import get_settings
from app.storage import result_store, scan_log, status_store

_ACTIVE = (ScanStatus.queued, ScanStatus.running)


class QueueFull(Exception):
    """Queued plus running scans have reached max_queue_depth."""


async def submit(redis: aioredis.Redis, body: ScanRequest, submitted: dict) -> str:
    """Queue a scan and return its id. An identical request (same dedup hash) that is still queued or running
    returns that scan's id, and nothing new is started."""
    settings = get_settings()
    in_flight = await status_store.running_count(redis) + await status_store.pending_count(redis)
    if in_flight >= settings.max_queue_depth:
        raise QueueFull()

    scan_id = "scn_" + str(ULID())
    request_hash = body.dedup_hash()
    holder = await status_store.claim_dedup(redis, request_hash, scan_id)
    if holder:
        if await status_store.get_status(redis, holder) in _ACTIVE:
            return holder
        await status_store.replace_dedup(redis, request_hash, scan_id)  # the previous scan is finished

    result_store.ensure_scan_dir(scan_id)
    scan_log.append_sync(settings.scan_log_path(scan_id), scan_id, "INFO", "scan accepted")
    now = datetime.datetime.now(datetime.UTC)
    await status_store.set_status(redis, scan_id, ScanStatus.queued)
    await status_store.set_meta(redis, scan_id, brand_name=body.brand_name, created_at=now)
    await status_store.add_pending(redis, scan_id, {**body.model_dump(), "submitted": submitted})
    await promote_pending(redis)  # starts it right away if a slot is free
    return scan_id


async def promote_pending(redis: aioredis.Redis) -> None:
    """Fill free slots of the running queue from the pending queue and hand those scans to the worker."""
    # Imported here because the worker module (app.jobs.actors) imports this one.
    from app.jobs.actors import scan_actor

    for scan_id in await status_store.reap_lost(redis):
        scan_log.append_sync(get_settings().scan_log_path(scan_id), scan_id, "ERROR", "worker lost; scan abandoned")
    for scan_id in await status_store.promote(redis):
        request_dict = await status_store.take_request(redis, scan_id)
        if request_dict is None:  # deleted while pending
            await status_store.remove_from_running(redis, scan_id)
            continue
        scan_actor.send(scan_id, request_dict)
