"""Integration tests for the FastAPI endpoints.

Uses TestClient with patched status_store/result_store functions
to avoid Redis connection issues in CI.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.models import ScanStatus


@pytest.fixture(autouse=True)
def reset_settings():
    """Reset the settings singleton between tests."""
    import app.config.settings as s_mod
    s_mod._settings = None
    yield
    s_mod._settings = None


@pytest.fixture
def client(tmp_path: Path):
    data_dir = tmp_path / "data"
    (data_dir / "results").mkdir(parents=True)
    (data_dir / "tmp").mkdir(parents=True)
    uploads_dir = tmp_path / "uploads"
    uploads_dir.mkdir()

    with patch.dict(os.environ, {
        "DATA_DIR": str(data_dir),
        "UPLOADS_DIR": str(uploads_dir),
        "API_KEY": "test-key",
        "REDIS_URL": "redis://localhost:6379/15",
        "WEB_CONCURRENCY": "1",
    }):
        import app.config.settings as s_mod
        s_mod._settings = None

        from app.api.routes.scans import router
        from app.jobs import actors as actors_mod

        # Track state in memory for the mock
        state: dict = {}

        mock_redis = MagicMock()

        async def mock_get_dedup(redis, hash_val):
            return state.get(f"dedup:{hash_val}")

        async def mock_set_dedup(redis, hash_val, scan_id):
            state[f"dedup:{hash_val}"] = scan_id

        async def mock_get_status(redis, scan_id):
            v = state.get(f"status:{scan_id}")
            return ScanStatus(v) if v else None

        async def mock_set_status(redis, scan_id, s, **kwargs):
            state[f"status:{scan_id}"] = s.value

        async def mock_set_meta(redis, scan_id, brand_name, created_at, **kwargs):
            state[f"meta:{scan_id}"] = {"brand_name": brand_name, "created_at": created_at.isoformat(), "queue_position": 1, "warnings": []}

        async def mock_get_meta(redis, scan_id):
            return state.get(f"meta:{scan_id}")

        async def mock_update_meta_field(redis, scan_id, **fields):
            meta = state.get(f"meta:{scan_id}", {})
            meta.update(fields)
            state[f"meta:{scan_id}"] = meta

        async def mock_get_running_ids(redis):
            return list(state.get("running", set()))

        async def mock_add_to_running(redis, scan_id):
            state.setdefault("running", set()).add(scan_id)

        async def mock_remove_from_running(redis, scan_id):
            state.get("running", set()).discard(scan_id)

        async def mock_get_progress(redis, scan_id):
            return state.get(f"progress:{scan_id}", 0)

        async def mock_signal_cancel(redis, scan_id):
            state[f"cancel:{scan_id}"] = True

        async def mock_delete_all(redis, scan_id):
            for key in [f"status:{scan_id}", f"meta:{scan_id}", f"progress:{scan_id}", f"cancel:{scan_id}"]:
                state.pop(key, None)
            state.get("running", set()).discard(scan_id)

        async def mock_get_sources(redis, scan_id):
            return []

        import app.storage.status_store as ss
        patches = [
            patch.object(ss, "get_dedup", side_effect=mock_get_dedup),
            patch.object(ss, "set_dedup", side_effect=mock_set_dedup),
            patch.object(ss, "get_status", side_effect=mock_get_status),
            patch.object(ss, "set_status", side_effect=mock_set_status),
            patch.object(ss, "set_meta", side_effect=mock_set_meta),
            patch.object(ss, "get_meta", side_effect=mock_get_meta),
            patch.object(ss, "update_meta_field", side_effect=mock_update_meta_field),
            patch.object(ss, "get_running_ids", side_effect=mock_get_running_ids),
            patch.object(ss, "add_to_running", side_effect=mock_add_to_running),
            patch.object(ss, "remove_from_running", side_effect=mock_remove_from_running),
            patch.object(ss, "get_progress", side_effect=mock_get_progress),
            patch.object(ss, "signal_cancel", side_effect=mock_signal_cancel),
            patch.object(ss, "delete_all", side_effect=mock_delete_all),
            patch.object(ss, "get_sources", side_effect=mock_get_sources),
            patch.object(actors_mod.scan_actor, "send", return_value=None),
        ]

        app = FastAPI()
        app.include_router(router)
        app.state.redis = mock_redis
        app.state._test_state = state

        for p in patches:
            p.start()

        with TestClient(app, raise_server_exceptions=True) as c:
            c._test_state = state
            yield c

        for p in patches:
            p.stop()


HEADERS = {"X-API-Key": "test-key"}
VALID_BODY = {
    "brand_name": "Petpooja",
    "android_packages": ["com.petpooja.billing"],
    "keywords": ["petpooja"],
}


def test_create_scan_returns_202(client):
    resp = client.post("/v1/scans", json=VALID_BODY, headers=HEADERS)
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "queued"
    assert data["scan_id"].startswith("scn_")
    assert "expanded_keywords" in data
    assert len(data["expanded_keywords"]) > 0


def test_create_scan_empty_keywords_422(client):
    body = {"brand_name": "Test", "keywords": []}
    resp = client.post("/v1/scans", json=body, headers=HEADERS)
    assert resp.status_code == 422


def test_create_scan_no_keywords_422(client):
    body = {"brand_name": "Test"}
    resp = client.post("/v1/scans", json=body, headers=HEADERS)
    assert resp.status_code == 422


def test_create_scan_apk_path_traversal_rejected(client):
    body = {**VALID_BODY, "apk_path": "../../../etc/passwd"}
    resp = client.post("/v1/scans", json=body, headers=HEADERS)
    assert resp.status_code == 400


def test_get_scan_status_not_found(client):
    resp = client.get("/v1/scans/scn_doesnotexist", headers=HEADERS)
    assert resp.status_code == 404


def test_get_scan_result_not_found(client):
    resp = client.get("/v1/scans/scn_doesnotexist/result", headers=HEADERS)
    assert resp.status_code == 404


def test_cancel_not_found(client):
    resp = client.post("/v1/scans/scn_doesnotexist/cancel", headers=HEADERS)
    assert resp.status_code == 404


def test_delete_not_found(client):
    resp = client.delete("/v1/scans/scn_doesnotexist", headers=HEADERS)
    assert resp.status_code == 404


def test_get_queue(client):
    resp = client.get("/v1/queue", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert "pending_count" in data
    assert "running" in data
    assert "max_concurrent" in data


def test_list_scans(client):
    resp = client.get("/v1/scans", headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert "scans" in data
    assert "total" in data


def test_create_scan_full_body(client):
    body = {
        "brand_name": "Petpooja",
        "android_packages": ["com.petpooja.billing"],
        "ios_bundles": ["com.petpooja.billing"],
        "official_domain": "petpooja.com",
        "keywords": ["petpooja", "billing"],
        "analyze_apks": False,
        "depth": 2,
        "callback_url": "https://webhook.example.com/notify",
    }
    resp = client.post("/v1/scans", json=body, headers=HEADERS)
    assert resp.status_code == 202
    assert resp.json()["scan_id"].startswith("scn_")


def test_scan_deduplication(client):
    resp1 = client.post("/v1/scans", json=VALID_BODY, headers=HEADERS)
    assert resp1.status_code == 202
    scan_id_1 = resp1.json()["scan_id"]

    resp2 = client.post("/v1/scans", json=VALID_BODY, headers=HEADERS)
    assert resp2.status_code == 202
    scan_id_2 = resp2.json()["scan_id"]

    assert scan_id_1 == scan_id_2, "Identical requests should return the same scan_id"


def test_get_status_after_create(client):
    resp = client.post("/v1/scans", json=VALID_BODY, headers=HEADERS)
    scan_id = resp.json()["scan_id"]

    status_resp = client.get(f"/v1/scans/{scan_id}", headers=HEADERS)
    assert status_resp.status_code == 200
    data = status_resp.json()
    assert data["scan_id"] == scan_id
    assert data["status"] == "queued"


def test_cancel_queued_scan(client):
    resp = client.post("/v1/scans", json=VALID_BODY, headers=HEADERS)
    scan_id = resp.json()["scan_id"]
    cancel_resp = client.post(f"/v1/scans/{scan_id}/cancel", headers=HEADERS)
    assert cancel_resp.status_code == 202


def test_delete_running_scan_returns_409(client):
    """Deleting a running scan should return 409."""
    resp = client.post("/v1/scans", json=VALID_BODY, headers=HEADERS)
    scan_id = resp.json()["scan_id"]

    # Force status to running in the mock state
    client._test_state[f"status:{scan_id}"] = "running"

    delete_resp = client.delete(f"/v1/scans/{scan_id}", headers=HEADERS)
    assert delete_resp.status_code == 409


def test_openapi_schema_accessible(client):
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    schema = resp.json()
    assert "paths" in schema
    assert "/v1/scans" in schema["paths"]
