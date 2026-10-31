# app/services/brand_store.py
"""Brand records: discovery input, candidate list and the customer's confirmed ids.

One JSON file per brand under DATA_DIR/brands/<brand_id>.json, written atomically. A record moves through
  queued -> running -> done (candidates ready for confirmation)   or   failed (error set)

Confirmation stores, per item, the store, id, name, publisher, who confirmed it and when. Only confirmed items
reach a scan: apply_to_request() adds them to the scan request, so the publisher-plus-package rule applies
to them on their own platform.
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any

from app.api.models import ScanRequest
from app.config.settings import get_settings

# Who confirmed an app that came from the customer's own link (no separate step needed)
LINK_CONFIRMER = "customer link"
_ID_RE = re.compile(r"^brd_[0-9A-HJKMNP-TV-Z]{26}$")  # ULID with a brd_ prefix


def _dir() -> Path:
    path = get_settings().data_dir / "brands"
    path.mkdir(parents=True, exist_ok=True)
    return path


def valid_brand_id(brand_id: str) -> bool:
    return bool(_ID_RE.match(brand_id or ""))


def _path(brand_id: str) -> Path:
    if not valid_brand_id(brand_id):
        raise ValueError(f"invalid brand id {brand_id!r}")
    return _dir() / f"{brand_id}.json"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(record: dict[str, Any]) -> None:
    path = _path(record["brand_id"])
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def load(brand_id: str) -> dict[str, Any] | None:
    if not valid_brand_id(brand_id):
        return None
    path = _path(brand_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def create(brand_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    record = {
        "brand_id": brand_id,
        "status": "queued",
        "created_at": _now(),
        "input": payload,
        "candidates": [],
        "play_developer": None,
        "error": None,
        "confirmed": [],
        "confirmed_at": None,
    }
    save(record)
    return record


def _confirmed_item(cand: dict[str, Any], confirmed_by: str, now: str, linked: bool = False) -> dict[str, Any]:
    return {
        "store": cand["store"], "id": cand["id"], "name": cand["name"], "publisher": cand["publisher"],
        "confirmed_by": confirmed_by, "confirmed_at": now, "linked": linked,
    }


def set_candidates(record: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Store the candidates. The apps behind the customer's own links are confirmed at once: the customer gave them."""
    now = _now()
    linked = [_confirmed_item(c, LINK_CONFIRMER, now, linked=True) for c in result["candidates"] if c.get("linked")]
    record.update(
        status="done",
        candidates=result["candidates"],
        play_developer=result.get("play_developer"),
        notes=result.get("notes", []),
        error=None,
        confirmed=linked,
        confirmed_at=now if linked else None,
    )
    save(record)
    return record


def set_failed(record: dict[str, Any], error: str) -> dict[str, Any]:
    record.update(status="failed", error=error)
    save(record)
    return record


def confirm(record: dict[str, Any], selected: list[dict[str, str]], confirmed_by: str) -> dict[str, Any]:
    """Confirm the selected candidates, on top of the apps behind the customer's links, which stay confirmed.

    Each selection must be a known candidate. Confirming again adds to the confirmed set; it does not replace it,
    so a linked app cannot be dropped by leaving it out of the selection.
    """
    known = {(c["store"], c["id"]): c for c in record["candidates"]}
    now = _now()
    items: dict[tuple[str, str], dict[str, Any]] = {(i["store"], i["id"]): i for i in record.get("confirmed", [])}
    for sel in selected:
        key = (sel["store"], sel["id"])
        cand = known.get(key)
        if cand is None:
            raise ValueError(f"{sel['store']} id {sel['id']} is not in the candidate list")
        items.setdefault(key, _confirmed_item(cand, confirmed_by, now))
    record.update(confirmed=list(items.values()), confirmed_at=now)
    save(record)
    return record


def confirmed_publishers(record: dict[str, Any]) -> list[str]:
    seen: dict[str, None] = {}
    for item in record.get("confirmed", []):
        if item.get("publisher"):
            seen.setdefault(item["publisher"], None)
    return list(seen)


def apply_to_request(body: ScanRequest, record: dict[str, Any]) -> ScanRequest:
    """A copy of the scan request with the confirmed ids, publishers and domains added."""
    android = list(body.android_packages)
    ios = list(body.ios_bundles)
    for item in record.get("confirmed", []):
        if item["store"] == "play":
            android.append(item["id"])
        elif item["store"] == "app_store":
            ios.append(item["id"])
    publishers = [*body.official_publishers, *confirmed_publishers(record)]
    aliases = [*body.brand_aliases, *record.get("input", {}).get("aliases", [])]
    domains = list(body.official_domains)
    input_domains = record.get("input", {}).get("official_domains", [])
    domains.extend(d for d in input_domains if d not in domains)
    official_domain = body.official_domain or (input_domains[0] if input_domains else None)
    return body.model_copy(update={
        "android_packages": list(dict.fromkeys(android)),
        "ios_bundles": list(dict.fromkeys(ios)),
        "official_publishers": list(dict.fromkeys(publishers)),
        "brand_aliases": list(dict.fromkeys(aliases)),
        "official_domains": domains,
        "official_domain": official_domain,
    })
