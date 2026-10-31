"""Shared pytest fixtures."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from fastapi.testclient import TestClient

os.environ.setdefault("DATA_DIR", str(Path(tempfile.mkdtemp()) / "data"))
os.environ.setdefault("UPLOADS_DIR", str(Path(tempfile.mkdtemp()) / "uploads"))
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")  # overridden by fakeredis
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("GITHUB_TOKEN", "")


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis(decode_responses=False)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    (d / "results").mkdir()
    (d / "tmp").mkdir()
    return d


@pytest.fixture
def sample_request() -> dict:
    return {
        "brand_name": "Petpooja",
        "android_packages": ["com.petpooja.billing"],
        "ios_bundles": ["com.petpooja.billing"],
        "official_domain": "petpooja.com",
        "keywords": ["petpooja", "billing", "restaurant"],
        "depth": 2,
    }
