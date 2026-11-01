# app/storage/brand_index.py
"""Brand details in Elasticsearch: one document per brand, _id = brand_id.

Index: {ELASTICSEARCH_INDEX_PREFIX}-brands (default "rogue-brands"). A document holds the fields of the
POST /v1/brand-details body, plus brand_id, allowlisted_apps, created_at and updated_at. The index is created on
first use.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

from elasticsearch import BadRequestError, NotFoundError
from elasticsearch import ConnectionError as ESConnectionError
from elasticsearch import TransportError
from ulid import ULID

from app.config.settings import Settings, get_settings
from app.api.models import ScanReport, ScanRequest
from app.storage import es_store

_ID_RE = re.compile(r"^brd_[0-9A-HJKMNP-TV-Z]{26}$")  # ULID with a brd_ prefix

BRAND_MAPPING: dict[str, Any] = {
    "dynamic": False,
    "properties": {
        "brand_id": {"type": "keyword"},
        "brand_name": {"type": "text", "fields": {"raw": {"type": "keyword", "ignore_above": 256}}},
        "play_store_urls": {"type": "keyword"},
        "app_store_urls": {"type": "keyword"},
        "official_domains": {"type": "keyword"},
        "aliases": {"type": "keyword"},
        "country": {"type": "keyword"},
        "allowlisted_apps": {"properties": {
            "store": {"type": "keyword"},
            "id": {"type": "keyword"},
            "name": {"type": "text"},
            "publisher": {"type": "keyword"},
        }},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
    },
}


class BrandNotFound(Exception):
    """No brand with that id."""


class BrandIndexUnavailable(Exception):
    """Elasticsearch could not be reached or refused the request."""


def index_name(settings: Settings | None = None) -> str:
    return f"{(settings or get_settings()).elasticsearch_index_prefix}-brands"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


async def _ensure_index(client: Any, index: str) -> None:
    if await client.indices.exists(index=index):
        return
    try:
        await client.indices.create(index=index, mappings=BRAND_MAPPING)
    except BadRequestError:
        pass  # another request created it first


async def _with_client(action):
    """Run action(client, index) against Elasticsearch. Connection problems become BrandIndexUnavailable."""
    settings = get_settings()
    client = es_store._client(settings)
    try:
        index = index_name(settings)
        await _ensure_index(client, index)
        return await action(client, index)
    except (ESConnectionError, TransportError) as exc:
        raise BrandIndexUnavailable(f"brand store unavailable: {exc}") from exc
    finally:
        await client.close()


def valid_brand_id(brand_id: str) -> bool:
    return bool(_ID_RE.match(brand_id or ""))


def _check_id(brand_id: str) -> None:
    if not valid_brand_id(brand_id):
        raise BrandNotFound(brand_id)


async def create(details: dict[str, Any]) -> dict[str, Any]:
    """Store a new brand under a new unique id (brd_ + ULID)."""
    brand_id = "brd_" + str(ULID())
    now = _now()
    doc = {"brand_id": brand_id, **details, "allowlisted_apps": [], "created_at": now, "updated_at": now}

    async def action(client, index):
        await client.index(index=index, id=brand_id, document=doc, refresh="wait_for")
        return doc

    return await _with_client(action)


async def read(brand_id: str) -> dict[str, Any]:
    _check_id(brand_id)

    async def action(client, index):
        try:
            resp = await client.get(index=index, id=brand_id)
        except NotFoundError as exc:
            raise BrandNotFound(brand_id) from exc
        return resp["_source"]

    return await _with_client(action)


async def update(brand_id: str, details: dict[str, Any]) -> dict[str, Any]:
    """Replace the brand's details. created_at is kept; updated_at is set to now."""
    _check_id(brand_id)

    async def action(client, index):
        try:
            current = (await client.get(index=index, id=brand_id))["_source"]
        except NotFoundError as exc:
            raise BrandNotFound(brand_id) from exc
        doc = {
            "brand_id": brand_id, **details, "allowlisted_apps": current.get("allowlisted_apps", []),
            "created_at": current["created_at"], "updated_at": _now(),
        }
        await client.index(index=index, id=brand_id, document=doc, refresh="wait_for")
        return doc

    return await _with_client(action)


async def delete(brand_id: str) -> None:
    _check_id(brand_id)

    async def action(client, index):
        try:
            await client.delete(index=index, id=brand_id, refresh=True)
        except NotFoundError as exc:
            raise BrandNotFound(brand_id) from exc

    await _with_client(action)


_OFFICIAL_SOURCES = {"Google Play": "play", "Apple App Store": "app_store"}


def official_from_report(report: ScanReport, request: ScanRequest) -> list[dict[str, str]]:
    """The brand's own apps this scan allowlisted, to keep on the brand record for later scans.

    Only apps listed on their own store (Google Play or the App Store) count, and only when the id is one the
    brand declares for that platform and the publisher is known. A copy on another store is never kept.
    """
    ids = request.official_app_ids()
    found: dict[tuple[str, str], dict[str, str]] = {}
    for item in report.allowlisted:
        store = _OFFICIAL_SOURCES.get(item.source or "")
        if not store or not item.package_id or not item.publisher:
            continue
        platform = "android" if store == "play" else "ios"
        if item.package_id.strip().lower() not in ids[platform]:
            continue
        found.setdefault((store, item.package_id), {
            "store": store, "id": item.package_id, "name": item.app_name or "", "publisher": item.publisher,
        })
    return list(found.values())


async def record_allowlist(brand_id: str, apps: list[dict[str, str]]) -> dict[str, Any]:
    """Keep apps on the brand record. Each (store, id) is stored once; the first record is kept."""
    _check_id(brand_id)

    async def action(client, index):
        try:
            current = (await client.get(index=index, id=brand_id))["_source"]
        except NotFoundError as exc:
            raise BrandNotFound(brand_id) from exc
        kept = {(a["store"], a["id"]): a for a in current.get("allowlisted_apps", [])}
        for app in apps:
            kept.setdefault((app["store"], app["id"]), app)
        doc = {**current, "allowlisted_apps": list(kept.values())}
        await client.index(index=index, id=brand_id, document=doc, refresh="wait_for")
        return doc

    return await _with_client(action)


def merge_into_request(body: ScanRequest, doc: dict[str, Any], own_apps: list[dict[str, str]]) -> ScanRequest:
    """The scan request with a stored brand's details added. Values the request already gives are kept.

    own_apps are the apps behind the brand's store links, read from their stores by the caller (discovery.listed_app).
    Apps kept on the brand record by earlier scans are added as well. Each app adds its id on its platform and its
    publisher. The store accounts of the own_apps (their developer_id) are added too, so every app those accounts
    publish is allowlisted. The brand's name, aliases and official domains are added too, and the brand name is
    taken from the stored brand when the request has none.
    """
    apps = [*own_apps, *doc.get("allowlisted_apps", [])]
    domains = doc.get("official_domains", [])
    play = [a["id"] for a in apps if a["store"] == "play"]
    ios = [a["id"] for a in apps if a["store"] == "app_store"]
    publishers = [a["publisher"] for a in apps if a.get("publisher")]
    # Only from own_apps: those were just read from the store behind the customer's own links
    play_devs = [a["developer_id"] for a in own_apps if a["store"] == "play" and a.get("developer_id")]
    sellers = [a["developer_id"] for a in own_apps if a["store"] == "app_store" and a.get("developer_id")]
    return body.model_copy(update={
        "brand_id": doc["brand_id"],
        "brand_name": body.brand_name or doc["brand_name"],
        "brand_aliases": list(dict.fromkeys([*body.brand_aliases, *doc.get("aliases", [])])),
        "android_packages": list(dict.fromkeys([*body.android_packages, *play])),
        "ios_bundles": list(dict.fromkeys([*body.ios_bundles, *ios])),
        "official_publishers": list(dict.fromkeys([*body.official_publishers, *publishers])),
        "play_developer_ids": list(dict.fromkeys([*body.play_developer_ids, *play_devs])),
        "app_store_seller_ids": list(dict.fromkeys([*body.app_store_seller_ids, *sellers])),
        "official_domains": list(dict.fromkeys([*body.official_domains, *domains])),
        "official_domain": body.official_domain or (domains[0] if domains else None),
    })
