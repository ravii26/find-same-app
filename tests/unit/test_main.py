"""Unit tests for app/main.py single-worker enforcement and app factory."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


def test_enforce_single_worker_exits_on_multi_worker(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    from app.main import _enforce_single_worker

    with pytest.raises(SystemExit) as exc_info:
        _enforce_single_worker()
    assert exc_info.value.code == 1


def test_enforce_single_worker_ok_with_one(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    from app.main import _enforce_single_worker

    # Should not raise
    _enforce_single_worker()


def test_enforce_single_worker_default_env(monkeypatch):
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    from app.main import _enforce_single_worker

    # Default is 1, should not raise
    _enforce_single_worker()


def test_create_app_returns_fastapi_instance():
    from app.main import create_app
    app = create_app()
    from fastapi import FastAPI
    assert isinstance(app, FastAPI)
    assert app.title == "Rogue App Discovery API"


def test_app_has_health_route():
    from app.main import create_app

    app = create_app()
    routes = [r.path for r in app.routes]
    assert "/health" in routes


def test_app_has_scan_routes():
    from app.main import create_app

    app = create_app()
    paths = [r.path for r in app.routes]
    assert any("/v1/scans" in p for p in paths)


def test_health_endpoint_redis_ok():
    """Test the /health route when Redis ping succeeds."""
    from app.main import create_app

    app = create_app()
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)
    app.state.redis = mock_redis

    # Bypass lifespan by patching _lifespan context manager
    with patch("app.main._lifespan", return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=None),
    )):
        with TestClient(app, raise_server_exceptions=True) as client:
            client.app.state.redis = mock_redis
            resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert data["redis"] == "ok"


def test_health_endpoint_redis_fail():
    """Test the /health route when Redis ping fails."""
    from app.main import create_app

    app = create_app()
    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(side_effect=Exception("connection refused"))
    app.state.redis = mock_redis

    with patch("app.main._lifespan", return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=None),
        __aexit__=AsyncMock(return_value=None),
    )):
        with TestClient(app, raise_server_exceptions=True) as client:
            client.app.state.redis = mock_redis
            resp = client.get("/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "degraded"
    assert data["redis"] == "error"


def test_app_module_level_instance():
    """The module-level `app` object is a FastAPI instance."""
    from app.main import app
    from fastapi import FastAPI
    assert isinstance(app, FastAPI)
