# app/storage/brand_index.py
"""Brand details in Elasticsearch: one document per brand, keyed by its name.

Index: {ELASTICSEARCH_INDEX_PREFIX}-brands (default "rogue-brands"). The document _id is the brand name, lower-cased
with spaces collapsed, so a second brand with the same name cannot be created: Elasticsearch refuses the write.
A brand is read, changed and deleted by its name. Allowlisted apps are not stored here for now.
"""

from __future__ import annotations

import datetime
from typing import Any

from elasticsearch import BadRequestError, ConflictError, NotFoundError, TransportError
from elasticsearch import ConnectionError as ESConnectionError

from app.api.models import ScanRequest
from app.config.settings import Settings, get_settings
from app.storage import es_store

BRAND_MAPPING: dict[str, Any] = {
    "dynamic": False,
    "properties": {
        "brand_name": {"type": "text", "fields": {"raw": {"type": "keyword", "ignore_above": 256}}},
        "play_store_urls": {"type": "keyword"},
        "app_store_urls": {"type": "keyword"},
        "official_domains": {"type": "keyword"},
        "aliases": {"type": "keyword"},
        "keywords": {"type": "keyword"},
        "country": {"type": "keyword"},
        "created_at": {"type": "date"},
        "updated_at": {"type": "date"},
    },
}


class BrandNotFound(Exception):
    """No brand with that name."""


class BrandExists(Exception):
    """A brand with that name is already stored."""


class BrandIndexUnavailable(Exception):
    """Elasticsearch could not be reached or refused the request."""


def index_name(settings: Settings | None = None) -> str:
    return f"{(settings or get_settings()).elasticsearch_index_prefix}-brands"


def name_key(name: str) -> str:
    """The unique key of a brand name: case and extra spaces do not make a second brand."""
    return " ".join(name.split()).casefold()


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


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


async def _fetch(client: Any, index: str, name: str) -> dict[str, Any]:
    key = name_key(name)
    if not key:
        raise BrandNotFound(name)
    try:
        resp = await client.get(index=index, id=key)
    except NotFoundError as exc:
        raise BrandNotFound(name) from exc
    return resp["_source"]


async def create(details: dict[str, Any]) -> dict[str, Any]:
    """Store a new brand. The name must not be stored already."""
    now = _now()
    doc = {**details, "created_at": now, "updated_at": now}
    key = name_key(details["brand_name"])

    async def action(client, index):
        try:
            await client.create(index=index, id=key, document=doc, refresh="wait_for")
        except ConflictError as exc:
            raise BrandExists(details["brand_name"]) from exc
        return doc

    return await _with_client(action)


async def read(name: str) -> dict[str, Any]:
    """The brand with this name."""
    async def action(client, index):
        return await _fetch(client, index, name)

    return await _with_client(action)


# Sort by name ignoring case: a keyword sort puts "bob World" after "Zomato". A script needs no mapping change, so
# brands stored before this sort existed are ordered too; an index without brand_name.raw sorts instead of failing.
_NAME_SORT = {"_script": {"type": "string", "order": "asc", "script": {"source": (
    "doc.containsKey('brand_name.raw') && doc['brand_name.raw'].size() > 0"
    " ? doc['brand_name.raw'].value.toLowerCase() : ''"
)}}}


async def list_all(page: int, size: int) -> dict[str, Any]:
    """One page of the stored brands, sorted by name, and how many brands are stored in all."""
    async def action(client, index):
        resp = await client.search(
            index=index, query={"match_all": {}}, sort=[_NAME_SORT],
            from_=(page - 1) * size, size=size, track_total_hits=True,
        )
        return {"total": resp["hits"]["total"]["value"], "brands": [h["_source"] for h in resp["hits"]["hits"]]}

    return await _with_client(action)


async def update(name: str, details: dict[str, Any]) -> dict[str, Any]:
    """Replace the brand's details. created_at is kept; updated_at is set to now.

    A new name moves the document to the new key. That fails with BrandExists when another brand has the name.
    """
    async def action(client, index):
        current = await _fetch(client, index, name)
        doc = {**details, "created_at": current["created_at"], "updated_at": _now()}
        current_key, new_key = name_key(name), name_key(details["brand_name"])
        if new_key == current_key:
            await client.index(index=index, id=current_key, document=doc, refresh="wait_for")
            return doc
        try:
            await client.create(index=index, id=new_key, document=doc, refresh="wait_for")
        except ConflictError as exc:
            raise BrandExists(details["brand_name"]) from exc
        await client.delete(index=index, id=current_key, refresh="wait_for")
        return doc

    return await _with_client(action)


async def delete(name: str) -> dict[str, Any]:
    """Remove the brand with this name and return what was removed."""
    async def action(client, index):
        current = await _fetch(client, index, name)
        try:
            await client.delete(index=index, id=name_key(name), refresh="wait_for")
        except NotFoundError as exc:
            raise BrandNotFound(name) from exc
        return current

    return await _with_client(action)


def merge_into_request(body: ScanRequest, doc: dict[str, Any], own_apps: list[dict[str, str]]) -> ScanRequest:
    """The scan request with a stored brand's details added. Values the request already gives are kept.

    own_apps are the apps behind the brand's store links, read from their stores by the caller (discovery.listed_app).
    Each app adds its id on its platform and its publisher. The store accounts of the own_apps (their developer_id)
    are added too, so every app those accounts publish is allowlisted. The brand's aliases, keywords and official
    domains are added too.
    """
    play = [a["id"] for a in own_apps if a["store"] == "play"]
    ios = [a["id"] for a in own_apps if a["store"] == "app_store"]
    publishers = [a["publisher"] for a in own_apps if a.get("publisher")]
    play_devs = [a["developer_id"] for a in own_apps if a["store"] == "play" and a.get("developer_id")]
    domains = doc.get("official_domains", [])
    return body.model_copy(update={
        "brand_name": doc["brand_name"],
        "brand_aliases": list(dict.fromkeys([*body.brand_aliases, *doc.get("aliases", [])])),
        "keywords": list(dict.fromkeys([*body.keywords, *doc.get("keywords", [])])),
        "android_packages": list(dict.fromkeys([*body.android_packages, *play])),
        "ios_bundles": list(dict.fromkeys([*body.ios_bundles, *ios])),
        "official_publishers": list(dict.fromkeys([*body.official_publishers, *publishers])),
        "play_developer_ids": list(dict.fromkeys([*body.play_developer_ids, *play_devs])),
        "official_domains": list(dict.fromkeys([*body.official_domains, *domains])),
        "official_domain": body.official_domain or (domains[0] if domains else None),
    })
