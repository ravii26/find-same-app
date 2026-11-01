# app/api/routes/scans.py
"""REST API: submit a scan, read its progress, delete it."""

from __future__ import annotations

import asyncio
import datetime
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from ulid import ULID

from app.api.middleware.auth import require_api_key
from app.api.models import ScanCreate, ScanRequest, ScanStatus
from app.config.settings import get_settings
from app.jobs.actors import promote_pending
from app.services import discovery
from app.storage import brand_index
from app.storage import result_store, scan_log, status_store

router = APIRouter(prefix="/v1", tags=["scans"])

_ACTIVE = (ScanStatus.queued, ScanStatus.running)


def _scan_id(scan_id: str) -> str:
    """Path-parameter guard. A malformed id (e.g. ".." arriving as %2E%2E) is simply 'not found'."""
    if not result_store.is_valid_scan_id(scan_id):
        raise HTTPException(status_code=404, detail="Scan not found")
    return scan_id


def _redis(request: Request) -> aioredis.Redis:
    return request.app.state.redis


def _source_summary(sources: list, total: int) -> dict:
    return {
        "total": total,
        "running": total - len(sources),
        "completed": sum(1 for s in sources if s.status in ("ok", "partial")),
        "failed": sum(1 for s in sources if s.status in ("failed", "blocked")),
        "skipped": sum(1 for s in sources if s.status == "skipped"),
    }


def _source_detail(sources: list) -> list:
    return [
        {"name": s.name, "status": s.status, "results": s.results, "duration_ms": s.duration_ms, "error": s.error}
        for s in sources
    ]


def _queued(scan_id: str, warning: str | None = None) -> JSONResponse:
    content = {"scan_id": scan_id, "message": "Job queued"}
    if warning:
        content["warning"] = warning
    return JSONResponse(status_code=202, content=content)


def _name_only_warning(brand_name: str) -> str:
    return (f"No brand named '{brand_name}' is stored, so this is a name-only scan: only apps named exactly like "
            f"the brand or pairing it with scam, mod or coupon wording are reported. Store the brand with "
            f"POST /v1/brand-details for a full scan.")


async def _own_apps(details: dict) -> list[dict[str, str]]:
    """The apps behind the brand's store links, read from their stores, so they are allowlisted for this scan.

    A store that cannot be read stops the scan: an app is not allowlisted on a guessed publisher.
    """
    links = [*details.get("play_store_urls", []), *details.get("app_store_urls", [])]
    try:
        return list(await asyncio.gather(*[discovery.listed_app(url, details.get("country", "in")) for url in links]))
    except discovery.DiscoveryError as exc:
        raise HTTPException(status_code=503, detail=f"could not read the brand's store listing: {exc}") from exc


# ── POST /v1/scans ────────────────────────────────────────────────────────────

@router.post("/scans", status_code=status.HTTP_202_ACCEPTED, response_model=None)
async def create_scan(
    payload: ScanCreate,
    request: Request,
    _: Annotated[str, Depends(require_api_key)],
):
    """Queue a scan. An identical request (same dedup_hash) that is still queued or
    running returns that scan instead of starting a second one."""
    settings = get_settings()
    redis = _redis(request)
    submitted = payload.model_dump(mode="json")
    body = ScanRequest(brand_name=payload.brand_name)

    # The brand's stored record is read by name. A name with no stored brand is a name-only scan.
    try:
        details = await brand_index.read(payload.brand_name)
    except brand_index.BrandNotFound:
        details = None
    except brand_index.BrandIndexUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if details:
        own_apps = await _own_apps(details)
        body = brand_index.merge_into_request(body, details, own_apps)
    body = body.model_copy(update={"submitted": submitted})
    warning = None if details else _name_only_warning(payload.brand_name)

    in_flight = await status_store.running_count(redis) + await status_store.pending_count(redis)
    if in_flight >= settings.max_queue_depth:
        raise HTTPException(status_code=429, detail="Queue is full", headers={"Retry-After": "60"})

    scan_id = "scn_" + str(ULID())
    request_hash = body.dedup_hash()
    holder = await status_store.claim_dedup(redis, request_hash, scan_id)
    if holder:
        holder_status = await status_store.get_status(redis, holder)
        if holder_status in _ACTIVE:
            return _queued(holder, warning)
        await status_store.replace_dedup(redis, request_hash, scan_id)  # previous scan is finished

    result_store.ensure_scan_dir(scan_id)
    scan_log.append_sync(settings.scan_log_path(scan_id), scan_id, "INFO", "scan accepted")
    now = datetime.datetime.now(datetime.timezone.utc)
    await status_store.set_status(redis, scan_id, ScanStatus.queued)
    await status_store.set_meta(redis, scan_id, brand_name=body.brand_name, created_at=now)
    await status_store.add_pending(redis, scan_id, {**body.model_dump(), "submitted": submitted})
    await promote_pending(redis)  # starts it right away if a slot is free
    return _queued(scan_id, warning)


# ── GET /v1/progress/{id} ────────────────────────────────────────────────────────

@router.get("/progress/{scan_id}", response_model=None)
async def get_progress(
    scan_id: Annotated[str, Depends(_scan_id)],
    request: Request,
    _: Annotated[str, Depends(require_api_key)],
):
    """Progress by scan id. Always returns the same shape; once done, status is the scan's own final status."""
    from app.sources.registry import get_all_adapters
    redis = _redis(request)
    s = await status_store.get_status(redis, scan_id)
    report = None
    if s is None:  # Redis is cold or the keys expired: the report on disk is authoritative
        report = result_store.read_result(scan_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Scan not found")
        s = ScanStatus(report.status or ScanStatus.completed.value)

    meta = await status_store.get_meta(redis, scan_id) or result_store.read_meta(scan_id) or {}
    total_sources = len(get_all_adapters())
    sources = await status_store.get_sources(redis, scan_id)
    base = {
        "scan_id": scan_id,
        "status": s.value,
        "brand_name": meta.get("brand_name"),
        "created_at": meta.get("created_at"),
        "started_at": meta.get("started_at"),
    }

    if s == ScanStatus.queued:
        return JSONResponse(content={
            **base, "queue_position": await status_store.queue_position(redis, scan_id), "overall_progress": 0,
        })

    if s == ScanStatus.running:
        done = {src.name for src in sources}
        pending = [
            {"name": a.name, "status": "pending", "results": None, "duration_ms": None, "error": None}
            for a in get_all_adapters() if a.name not in done
        ]
        return JSONResponse(content={
            **base,
            "phase": meta.get("phase", "collecting"),
            "overall_progress": round(len(sources) / total_sources * 100) if total_sources else 0,
            "sources": _source_summary(sources, total_sources),
            "source_detail": _source_detail(sources) + pending,
        })

    # Finished — derive status and return progress shape only (no results)
    # Finished: report the status the scan recorded, the same one the report and the webhook carry.
    # (Deriving it from source counts gave "partial" and "completed" for scans the report marks otherwise.)
    report = report or result_store.read_result(scan_id)
    if report is not None:
        sm = report.summary
        src_summary = {"total": sm.sources_total, "running": 0, "completed": sm.sources_ok,
                       "failed": sm.sources_failed, "skipped": sm.sources_skipped}
        final_status = report.status or s.value
    elif sources:
        src_summary = _source_summary(sources, total_sources)
        final_status = s.value
    else:
        src_summary = {"total": total_sources, "running": 0, "completed": 0, "failed": 0, "skipped": 0}
        final_status = s.value

    return JSONResponse(content={
        **base,
        "status": final_status,
        "overall_progress": 100,
        "sources": src_summary,
        "source_detail": _source_detail(sources),
        "finished_at": meta.get("finished_at"),
    })


# ── DELETE /v1/scans/{id} ─────────────────────────────────────────────────────

@router.delete("/scans/{scan_id}", response_model=None)
async def delete_scan(
    scan_id: Annotated[str, Depends(_scan_id)],
    request: Request,
    _: Annotated[str, Depends(require_api_key)],
):
    """Remove a job from the queue if it has not started. A job that is already running cannot be
    removed (409); a finished scan's data is deleted."""
    redis = _redis(request)
    s = await status_store.get_status(redis, scan_id)

    if s == ScanStatus.running:
        raise HTTPException(status_code=409, detail="Scan already started; it cannot be removed")

    deleted = result_store.delete_scan(scan_id)
    await status_store.delete_all(redis, scan_id)  # also leaves the pending/running queue
    from app.storage import es_store
    await es_store.delete_scan(scan_id)  # best-effort; no-op unless enabled
    if not deleted and s is None:
        raise HTTPException(status_code=404, detail="Scan not found")
    message = "Job removed from queue" if s == ScanStatus.queued else "Scan deleted"
    return JSONResponse(content={"scan_id": scan_id, "message": message})