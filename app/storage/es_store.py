# app/storage/es_store.py
"""Elasticsearch index of scan results.

output.json on disk stays the source of truth (see docs/DECISIONS.md); this
module mirrors each finished scan into Elasticsearch so hits can be searched
and aggregated across scans. Indexing is best-effort and never fails a scan.

Indices (names use ELASTICSEARCH_INDEX_PREFIX, default "rogue"):
  {prefix}-hits   one document per hit,  _id = "{scan_id}:{hit_id}"
  {prefix}-scans  one document per scan, _id = scan_id

Both are created from composable index templates on first use, with
`dynamic: false` so unexpected fields stay in _source without bloating the
mapping. Document IDs are deterministic, so Dramatiq retries and re-indexing
overwrite instead of duplicating.

Not indexed, deliberately: direct APK download URLs (docs/DECISIONS.md — no
download links in results), callback_url (may embed secrets) and apk_path.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any, Iterable

from app.api.models import Hit, ScanReport, ScanRequest
from app.config.settings import Settings, get_settings

log = logging.getLogger(__name__)

_TEMPLATE_VERSION = 1

_KW = {"type": "keyword", "ignore_above": 512}
_TEXT_KW = {"type": "text", "fields": {"raw": {"type": "keyword", "ignore_above": 256}}}

HITS_MAPPING: dict[str, Any] = {
    "dynamic": False,
    "properties": {
        "scan_id": {"type": "keyword"},
        "brand_name": _TEXT_KW,
        "scoring_ruleset": {"type": "keyword"},
        "id": {"type": "keyword"},
        "type": {"type": "keyword"},
        "title": _TEXT_KW,
        "app_name": _TEXT_KW,
        "package_id": _KW,
        "version": _KW,
        "publisher": _TEXT_KW,
        "icon_url": _KW,
        "url": _KW,
        "host": {"properties": {
            "group": {"type": "keyword"},
            "name": {"type": "keyword"},
            "domain": {"type": "keyword"},
            "is_official_store": {"type": "boolean"},
        }},
        "found_via": {"properties": {
            "source": {"type": "keyword"},
            "method": {"type": "keyword"},
            "query": _KW,
        }},
        "relevance_score": {"type": "integer"},
        "risk": {"type": "keyword"},
        "reasons": {"type": "text"},
        "is_official_match": {"type": "boolean"},
        "icon_similarity": {"type": "float"},
        "permission_similarity": {"type": "float"},
        "domain_similarity": {"properties": {
            "domain": {"type": "keyword"},
            "closest_official": {"type": "keyword"},
            "similarity": {"type": "float"},
        }},
        "evidence": {"properties": {
            "captured_at": {"type": "date"},
            "http_status": {"type": "integer"},
            "page_path": _KW,
            "page_sha256": {"type": "keyword"},
            "screenshot_path": _KW,
            "icon_path": _KW,
            "icon_sha256": {"type": "keyword"},
            "assessment": {"type": "keyword"},
            "error": {"type": "text"},
        }},
        "discovered_at": {"type": "date"},
        "scan_completed_at": {"type": "date"},
        "@timestamp": {"type": "date"},
    },
}

SCANS_MAPPING: dict[str, Any] = {
    "dynamic": False,
    "properties": {
        "scan_id": {"type": "keyword"},
        "status": {"type": "keyword"},
        "brand_name": _TEXT_KW,
        "android_package": _KW,
        "ios_bundle": _KW,
        "official_domain": _KW,
        "keywords": {"type": "keyword"},
        "capture_evidence": {"type": "boolean"},
        "total_results": {"type": "integer"},
        "high_risk_count": {"type": "integer"},
        "by_source_group": {"type": "flattened"},
        "sources_total": {"type": "integer"},
        "sources_ok": {"type": "integer"},
        "sources_failed": {"type": "integer"},
        "sources_skipped": {"type": "integer"},
        "allowlisted_count": {"type": "integer"},
        "suppressed_count": {"type": "integer"},
        "duration_ms": {"type": "long"},
        "scoring_ruleset": {"type": "keyword"},
        "warnings": {"type": "text"},
        "completed_at": {"type": "date"},
        "@timestamp": {"type": "date"},
    },
}

# Per-process cache of "templates already installed" — keyed by index prefix.
_templates_ready: set[str] = set()


def hits_index(settings: Settings | None = None) -> str:
    return f"{(settings or get_settings()).elasticsearch_index_prefix}-hits"


def scans_index(settings: Settings | None = None) -> str:
    return f"{(settings or get_settings()).elasticsearch_index_prefix}-scans"


def _risk(score: int, high_risk: int) -> str:
    return "high" if score >= high_risk else "medium"


def hit_document(
    hit: Hit, scan_id: str, brand_name: str, completed_at: datetime.datetime,
    ruleset: str, high_risk: int,
) -> dict[str, Any]:
    doc = hit.model_dump(mode="json", exclude={"apk_url"})
    doc.update(
        scan_id=scan_id,
        brand_name=brand_name,
        scoring_ruleset=ruleset,
        risk=_risk(hit.relevance_score, high_risk),
        scan_completed_at=completed_at.isoformat(),
        **{"@timestamp": completed_at.isoformat()},
    )
    return doc


def scan_document(
    report: ScanReport, request: ScanRequest, status: str,
    completed_at: datetime.datetime, high_risk: int,
) -> dict[str, Any]:
    doc = request.model_dump(mode="json", include={
        "brand_name", "android_package", "ios_bundle", "official_domain",
        "keywords", "capture_evidence",
    })
    doc.update(report.summary.model_dump(mode="json"))
    doc.update(
        scan_id=report.scan_id,
        status=status,
        high_risk_count=sum(1 for h in report.results if h.relevance_score >= high_risk),
        warnings=report.warnings,
        completed_at=completed_at.isoformat(),
        **{"@timestamp": completed_at.isoformat()},
    )
    return doc


def build_actions(
    report: ScanReport, request: ScanRequest, status: str,
    completed_at: datetime.datetime, high_risk: int, settings: Settings | None = None,
) -> Iterable[dict[str, Any]]:
    """Bulk actions for the scan document followed by one per hit."""
    s = settings or get_settings()
    yield {
        "_op_type": "index",
        "_index": scans_index(s),
        "_id": report.scan_id,
        "_source": scan_document(report, request, status, completed_at, high_risk),
    }
    for hit in report.results:
        yield {
            "_op_type": "index",
            "_index": hits_index(s),
            "_id": f"{report.scan_id}:{hit.id}",
            "_source": hit_document(
                hit, report.scan_id, request.brand_name, completed_at,
                report.summary.scoring_ruleset, high_risk,
            ),
        }


def _client(settings: Settings):
    from elasticsearch import AsyncElasticsearch

    kwargs: dict[str, Any] = {
        "hosts": [settings.elasticsearch_url],
        "request_timeout": settings.elasticsearch_timeout,
        "verify_certs": settings.elasticsearch_verify_certs,
    }
    if settings.elasticsearch_api_key:
        kwargs["api_key"] = settings.elasticsearch_api_key
    elif settings.elasticsearch_username:
        kwargs["basic_auth"] = (settings.elasticsearch_username, settings.elasticsearch_password)
    return AsyncElasticsearch(**kwargs)


async def ensure_templates(client: Any, settings: Settings) -> None:
    """Install (idempotently) the composable index templates for both indices."""
    prefix = settings.elasticsearch_index_prefix
    if prefix in _templates_ready:
        return
    shards = {
        "number_of_shards": settings.elasticsearch_shards,
        "number_of_replicas": settings.elasticsearch_replicas,
    }
    for name, index, mapping in (
        (f"{prefix}-hits", hits_index(settings), HITS_MAPPING),
        (f"{prefix}-scans", scans_index(settings), SCANS_MAPPING),
    ):
        await client.indices.put_index_template(
            name=name,
            index_patterns=[index],
            priority=100,
            version=_TEMPLATE_VERSION,
            template={"settings": shards, "mappings": mapping},
        )
    _templates_ready.add(prefix)


async def index_report(
    report: ScanReport, request: ScanRequest, status: str,
    completed_at: datetime.datetime | None = None,
) -> dict[str, int] | None:
    """Mirror a finished scan into Elasticsearch.

    Returns {"indexed": n, "failed": m}, or None when disabled/unavailable.
    Never raises.
    """
    settings = get_settings()
    from app.services.scoring import high_risk_threshold

    completed_at = completed_at or datetime.datetime.now(datetime.timezone.utc)
    client = None
    try:
        from elasticsearch.helpers import async_bulk

        client = _client(settings)
        await ensure_templates(client, settings)
        actions = list(build_actions(
            report, request, status, completed_at, high_risk_threshold(), settings,
        ))
        ok, errors = await async_bulk(
            client, actions, raise_on_error=False, stats_only=False, refresh=False,
        )
        failed = len(errors) if isinstance(errors, list) else int(errors)
        if failed:
            log.warning("elasticsearch: %d/%d documents failed for scan %s (first: %s)",
                        failed, len(actions), report.scan_id,
                        errors[0] if isinstance(errors, list) and errors else "n/a")
        return {"indexed": int(ok), "failed": failed}
    except Exception as exc:
        log.warning("elasticsearch: indexing failed for scan %s: %s", report.scan_id, exc)
        return None
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                log.warning("elasticsearch: closing client failed: %s", exc)


async def delete_scan(scan_id: str) -> bool:
    """Remove a scan's documents from both indices. Best-effort; True on success."""
    settings = get_settings()
    client = None
    try:
        client = _client(settings)
        query = {"term": {"scan_id": scan_id}}
        for index in (hits_index(settings), scans_index(settings)):
            await client.delete_by_query(
                index=index, query=query, ignore_unavailable=True,
                allow_no_indices=True, refresh=True, conflicts="proceed",
            )
        return True
    except Exception as exc:
        log.warning("elasticsearch: delete failed for scan %s: %s", scan_id, exc)
        return False
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                log.warning("elasticsearch: closing client failed: %s", exc)