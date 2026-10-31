# app/storage/status_store.py
"""
Redis-backed state for the scan lifecycle and the two scan queues.

Queue 1 (pending):  list  scan:pending:list   - scans waiting for a free slot (FIFO)
Queue 2 (running):  set   scan:running:set    - scans that own a slot; its size is capped at MAX_CONCURRENT_SCANS

promote() moves scans from queue 1 to queue 2 atomically (Lua) while queue 2 has room, and returns
the ids it moved so the caller can hand them to the worker. A finished scan leaves queue 2 and the
next pending scan takes its place.

Status/progress/meta keys have a TTL; results on disk are permanent. When Redis is cold and
output.json exists, the API reads the final state from the report instead.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import redis.asyncio as aioredis

from app.api.models import ScanStatus, SourceStatus
from app.config.settings import get_settings

_TERMINAL_STATUSES = {ScanStatus.completed, ScanStatus.completed_with_errors, ScanStatus.failed}
_REQUEST_TTL = 30 * 24 * 3600  # 30 days — safety-net GC for abandoned pending scans

_PENDING = "scan:pending:list"
_RUNNING = "scan:running:set"

# Move pending -> running while there is room (and start each moved scan's heartbeat).
# KEYS: pending, running. ARGV: limit, heartbeat ttl. Returns the moved ids.
_PROMOTE = """
local moved = {}
while redis.call('SCARD', KEYS[2]) < tonumber(ARGV[1]) do
  local id = redis.call('LPOP', KEYS[1])
  if not id then break end
  redis.call('SADD', KEYS[2], id)
  redis.call('SETEX', 'scan:alive:' .. id, ARGV[2], '1')
  moved[#moved + 1] = id
end
return moved
"""

_ALIVE_TTL = 120          # heartbeat refresh interval — running scan touches this every 30s
_INITIAL_ALIVE_TTL = 600  # grace period from promotion → Dramatiq picks up message and starts heartbeat


def _key(scan_id: str) -> str:
    return f"scan:status:{scan_id}"


def _progress_key(scan_id: str) -> str:
    return f"scan:progress:{scan_id}"


def _sources_key(scan_id: str) -> str:
    return f"scan:sources:{scan_id}"


def _meta_key(scan_id: str) -> str:
    return f"scan:meta:{scan_id}"


def _request_key(scan_id: str) -> str:
    return f"scan:request:{scan_id}"


def _alive_key(scan_id: str) -> str:
    return f"scan:alive:{scan_id}"


def _dedup_key(request_hash: str) -> str:
    return f"scan:dedup:{request_hash}"


async def set_status(redis: aioredis.Redis, scan_id: str, status: ScanStatus) -> None:
    # Active states persist indefinitely — a queued scan must not vanish from the API while waiting.
    # The 7-day retention clock only starts when the scan reaches a terminal state.
    if status in _TERMINAL_STATUSES:
        await redis.setex(_key(scan_id), get_settings().redis_status_ttl, status.value)
    else:
        await redis.set(_key(scan_id), status.value)


async def get_status(redis: aioredis.Redis, scan_id: str) -> ScanStatus | None:
    raw = await redis.get(_key(scan_id))
    return ScanStatus(raw.decode()) if raw is not None else None


async def set_progress(redis: aioredis.Redis, scan_id: str, pct: int) -> None:
    await redis.setex(_progress_key(scan_id), get_settings().redis_status_ttl, str(pct))


async def set_meta(
    redis: aioredis.Redis, scan_id: str, brand_name: str, created_at: datetime,
) -> None:
    payload = {"brand_name": brand_name, "created_at": created_at.isoformat()}
    await redis.setex(_meta_key(scan_id), get_settings().redis_status_ttl, json.dumps(payload))


async def get_meta(redis: aioredis.Redis, scan_id: str) -> dict[str, Any] | None:
    raw = await redis.get(_meta_key(scan_id))
    return json.loads(raw) if raw is not None else None


async def update_meta_field(redis: aioredis.Redis, scan_id: str, **fields: Any) -> None:
    meta = await get_meta(redis, scan_id) or {}
    meta.update({k: v.isoformat() if isinstance(v, datetime) else v for k, v in fields.items()})
    await redis.setex(_meta_key(scan_id), get_settings().redis_status_ttl, json.dumps(meta))


async def update_sources(redis: aioredis.Redis, scan_id: str, sources: list[SourceStatus]) -> None:
    payload = json.dumps([s.model_dump() for s in sources])
    await redis.setex(_sources_key(scan_id), get_settings().redis_status_ttl, payload)


async def get_sources(redis: aioredis.Redis, scan_id: str) -> list[SourceStatus]:
    raw = await redis.get(_sources_key(scan_id))
    return [SourceStatus(**s) for s in json.loads(raw)] if raw is not None else []


# ── Request de-duplication: identical in-flight requests share one scan ──────

async def claim_dedup(redis: aioredis.Redis, request_hash: str, scan_id: str) -> str | None:
    """Atomically claim the hash for scan_id. Returns the id already holding it, or None if claimed."""
    ttl = get_settings().redis_status_ttl
    if await redis.set(_dedup_key(request_hash), scan_id, nx=True, ex=ttl):
        return None
    raw = await redis.get(_dedup_key(request_hash))
    return raw.decode() if raw else None


async def replace_dedup(redis: aioredis.Redis, request_hash: str, scan_id: str) -> None:
    await redis.setex(_dedup_key(request_hash), get_settings().redis_status_ttl, scan_id)


# ── Queue 1: pending ─────────────────────────────────────────────────────────

async def add_pending(redis: aioredis.Redis, scan_id: str, request_dict: dict) -> int:
    """Store the request and append the scan to the pending queue; returns its 1-based position."""
    await redis.set(_request_key(scan_id), json.dumps(request_dict), ex=_REQUEST_TTL)
    return int(await redis.rpush(_PENDING, scan_id))


async def remove_pending(redis: aioredis.Redis, scan_id: str) -> None:
    await redis.lrem(_PENDING, 0, scan_id)


async def queue_position(redis: aioredis.Redis, scan_id: str) -> int:
    """1-based position in the pending queue, 0 if not pending."""
    idx = await redis.lpos(_PENDING, scan_id)
    return 0 if idx is None else idx + 1


async def pending_count(redis: aioredis.Redis) -> int:
    return int(await redis.llen(_PENDING))


async def take_request(redis: aioredis.Redis, scan_id: str) -> dict | None:
    raw = await redis.getdel(_request_key(scan_id))
    return json.loads(raw) if raw else None


# ── Queue 2: running ─────────────────────────────────────────────────────────

async def running_count(redis: aioredis.Redis) -> int:
    return int(await redis.scard(_RUNNING))


async def remove_from_running(redis: aioredis.Redis, scan_id: str) -> None:
    await redis.srem(_RUNNING, scan_id)
    await redis.delete(_alive_key(scan_id))


async def touch_alive(redis: aioredis.Redis, scan_id: str) -> None:
    await redis.setex(_alive_key(scan_id), _ALIVE_TTL, "1")


async def reap_lost(redis: aioredis.Redis) -> list[str]:
    """Free slots held by scans whose worker died (no heartbeat). Returns the reaped ids."""
    lost = []
    for raw in await redis.smembers(_RUNNING):
        scan_id = raw.decode()
        if not await redis.exists(_alive_key(scan_id)):
            await redis.srem(_RUNNING, scan_id)
            await set_status(redis, scan_id, ScanStatus.failed)
            lost.append(scan_id)
    return lost


async def promote(redis: aioredis.Redis) -> list[str]:
    """Move pending scans into the running queue while it has room; returns the moved ids."""
    limit = get_settings().max_concurrent_scans
    moved = await redis.eval(_PROMOTE, 2, _PENDING, _RUNNING, limit, _INITIAL_ALIVE_TTL)
    return [m.decode() for m in moved]


# ── Delete all keys for a scan ───────────────────────────────────────────────

async def delete_all(redis: aioredis.Redis, scan_id: str) -> None:
    await redis.delete(
        _key(scan_id), _progress_key(scan_id), _sources_key(scan_id), _meta_key(scan_id), _request_key(scan_id),
    )
    await remove_from_running(redis, scan_id)
    await remove_pending(redis, scan_id)