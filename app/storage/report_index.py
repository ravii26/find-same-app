# app/storage/report_index.py
"""Rogue apps reported by users, in Elasticsearch: one document per reported app or page of a brand.

Index: {ELASTICSEARCH_INDEX_PREFIX}-reports (default "rogue-reports"). Reports are stored as sent: nothing is fetched,
verified or scored here. A takedown system reads them later.

The document _id is the report's target key (brand + the reported app or page), so a second report of the same target
cannot create a second document: Elasticsearch refuses the write, and the submission is added to the stored report
instead. A Play link is keyed by its package and an App Store link by its app id, so the same listing in another
language or country ("?hl=en-IN") is one report. Any other link is keyed by its normalized URL.
"""

from __future__ import annotations

import datetime
from typing import Any

from elasticsearch import BadRequestError, ConflictError, TransportError
from elasticsearch import ConnectionError as ESConnectionError
from ulid import ULID

from app.config.settings import Settings, get_settings
from app.services.discovery import app_store_id_from_url, play_package_from_url
from app.storage import es_store
from app.storage.brand_index import name_key
from app.utils.url_tools import normalize_url, url_id

REPORT_MAPPING: dict[str, Any] = {
    "dynamic": False,
    "properties": {
        "report_id": {"type": "keyword"},
        "brand_name": {"type": "text", "fields": {"raw": {"type": "keyword", "ignore_above": 256}}},
        "url": {"type": "keyword", "ignore_above": 2048},
        "url_normalized": {"type": "keyword", "ignore_above": 2048},
        "target_key": {"type": "keyword"},
        "app_name": {"type": "text"},
        "package_id": {"type": "keyword"},
        "category": {"type": "keyword"},
        "country": {"type": "keyword"},
        "seen_at": {"type": "date"},
        # Kept with the report but not searchable: it holds the reporters' contact details
        "submissions": {"type": "object", "enabled": False},
        "report_count": {"type": "integer"},
        "status": {"type": "keyword"},
        "received_at": {"type": "date"},
        "updated_at": {"type": "date"},
    },
}

# Every submission is counted in report_count; only the first ones are kept in full
MAX_SUBMISSIONS = 100
# A repeat is added inside Elasticsearch, so reports of one target arriving together are all counted. Elasticsearch
# retries the change this many times when another submission changed the report first.
_RETRY_ON_CONFLICT = 50
# Adds one submission to the stored report. Values the report lacks are taken from the new report, and a report
# without a category ("other") takes the new one.
_ADD_SUBMISSION = """
def s = ctx._source;
for (f in params.fill.keySet()) {
  if (s.get(f) == null && params.fill.get(f) != null) { s.put(f, params.fill.get(f)); }
}
if (s.get('category') == null || s.category == 'other') { s.category = params.category; }
if (s.submissions.size() < params.max_submissions) { s.submissions.add(params.submission); }
s.report_count += 1;
s.updated_at = params.now;
"""


class ReportIndexUnavailable(Exception):
    """Elasticsearch could not be reached or refused the request."""


def index_name(settings: Settings | None = None) -> str:
    return f"{(settings or get_settings()).elasticsearch_index_prefix}-reports"


def target_key(brand_name: str, url: str) -> str:
    """What a report is about: the brand and the reported Play package, App Store app or normalized URL."""
    pkg = play_package_from_url(url)
    if pkg:
        target = f"play:{pkg.lower()}"
    elif app_id := app_store_id_from_url(url):
        target = f"app_store:{app_id}"
    else:
        target = f"url:{url_id(url)}"
    return f"{name_key(brand_name)}|{target}"


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


async def _ensure_index(client: Any, index: str) -> None:
    if await client.indices.exists(index=index):
        return
    try:
        await client.indices.create(index=index, mappings=REPORT_MAPPING)
    except BadRequestError:
        pass  # another request created it first


async def submit(report: dict[str, Any], submission: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Store a report, or add it to the stored report of the same target. Returns (stored report, created).

    report holds what the report is about: brand_name, url, app_name, package_id, category, country, seen_at.
    submission holds what this reporter sent: description, reporter reference and contact, evidence_urls.
    """
    now = _now()
    key = target_key(report["brand_name"], report["url"])
    new_doc = {
        **report,
        "report_id": "rpt_" + str(ULID()),
        "url_normalized": normalize_url(report["url"]),
        "target_key": key,
        "submissions": [{**submission, "received_at": now}],
        "report_count": 1,
        "status": "open",
        "received_at": now,
        "updated_at": now,
    }
    settings = get_settings()
    client = es_store._client(settings)
    try:
        index = index_name(settings)
        await _ensure_index(client, index)
        try:
            await client.create(index=index, id=key, document=new_doc, refresh="wait_for")
            return new_doc, True
        except ConflictError:
            pass  # already reported: add this submission to that report
        params = {
            "fill": {f: report.get(f) for f in ("app_name", "package_id", "country", "seen_at")},
            "category": report["category"],
            "submission": {**submission, "received_at": now},
            "max_submissions": MAX_SUBMISSIONS,
            "now": now,
        }
        resp = await client.update(
            index=index, id=key, script={"source": _ADD_SUBMISSION, "params": params},
            retry_on_conflict=_RETRY_ON_CONFLICT, refresh="wait_for", source=True,
        )
        return resp["get"]["_source"], False
    except ConflictError as exc:  # still changing after every retry
        raise ReportIndexUnavailable(f"report {key} kept changing; the submission was not added") from exc
    except (ESConnectionError, TransportError) as exc:
        raise ReportIndexUnavailable(f"report store unavailable: {exc}") from exc
    finally:
        await client.close()
