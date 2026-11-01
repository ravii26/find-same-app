"""Brand details CRUD: the storage functions and the four routes, against an in-memory stand-in for Elasticsearch."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from elastic_transport import ConnectionError as TransportConnectionError
from elasticsearch import NotFoundError
from fastapi.testclient import TestClient

from app.api.models import ScanRequest
from app.config.settings import get_settings
from app.main import app
from app.storage import brand_index, es_store

BODY = {
    "brand_name": "YONO SBI",
    "play_store_url": "https://play.google.com/store/apps/details?id=com.sbi.lotusintouch",
    "aliases": ["YONO"],
    "official_domains": ["sbi.co.in"],
    "country": "in",
}
PLAY = "https://play.google.com/store/apps/details?id=com.sbi.lotusintouch"


def _not_found():
    return NotFoundError("not found", meta=MagicMock(status=404), body={})


class FakeES:
    """The few client calls brand_index makes, backed by a dict."""

    def __init__(self, fail=False):
        self.docs: dict[str, dict] = {}
        self.indices = self
        self.fail = fail

    def _check(self):
        if self.fail:
            raise TransportConnectionError("elasticsearch is down")

    async def exists(self, index):
        self._check()
        return True

    async def create(self, index, mappings):
        pass

    async def index(self, index, id, document, refresh=None):
        self._check()
        self.docs[id] = document

    async def get(self, index, id):
        self._check()
        if id not in self.docs:
            raise _not_found()
        return {"_source": self.docs[id]}

    async def delete(self, index, id, refresh=None):
        self._check()
        if id not in self.docs:
            raise _not_found()
        del self.docs[id]

    async def close(self):
        pass


@pytest.fixture
def es(monkeypatch):
    client = FakeES()
    monkeypatch.setattr(es_store, "_client", lambda settings: client)
    return client


def _details(**changes):
    return {**_details_body(), **changes}


def _details_body():
    from app.api.routes.brand_details import BrandDetailsRequest
    return BrandDetailsRequest(**{**BODY, "play_store_url": None, "play_store_urls": [PLAY]}).model_dump()


# ── storage functions ─────────────────────────────────────────────────────────

async def test_create_gives_a_new_brd_id_and_stores_the_details(es):
    doc = await brand_index.create(_details_body())
    assert doc["brand_id"].startswith("brd_") and len(doc["brand_id"]) == 30
    assert es.docs[doc["brand_id"]]["brand_name"] == "YONO SBI"
    assert doc["created_at"] == doc["updated_at"]


async def test_each_brand_gets_its_own_id(es):
    first = await brand_index.create(_details_body())
    second = await brand_index.create(_details_body())
    assert first["brand_id"] != second["brand_id"]


async def test_read_returns_what_was_stored(es):
    created = await brand_index.create(_details_body())
    assert await brand_index.read(created["brand_id"]) == created


async def test_read_of_an_unknown_brand_is_not_found(es):
    with pytest.raises(brand_index.BrandNotFound):
        await brand_index.read("brd_01HZX0000000000000000000AB")


async def test_malformed_id_is_not_found_without_a_query(es):
    with pytest.raises(brand_index.BrandNotFound):
        await brand_index.read("not-an-id")


async def test_update_replaces_details_and_keeps_created_at(es):
    created = await brand_index.create(_details_body())
    updated = await brand_index.update(created["brand_id"], _details(brand_name="YONO SBI Group"))
    assert updated["brand_name"] == "YONO SBI Group"
    assert updated["created_at"] == created["created_at"]
    assert updated["brand_id"] == created["brand_id"]


async def test_update_of_an_unknown_brand_is_not_found(es):
    with pytest.raises(brand_index.BrandNotFound):
        await brand_index.update("brd_01HZX0000000000000000000AB", _details_body())


async def test_delete_removes_the_brand(es):
    created = await brand_index.create(_details_body())
    await brand_index.delete(created["brand_id"])
    with pytest.raises(brand_index.BrandNotFound):
        await brand_index.read(created["brand_id"])
    with pytest.raises(brand_index.BrandNotFound):
        await brand_index.delete(created["brand_id"])


async def test_unreachable_elasticsearch_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setattr(es_store, "_client", lambda settings: FakeES(fail=True))
    with pytest.raises(brand_index.BrandIndexUnavailable):
        await brand_index.create(_details_body())


# ── routes ────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(es):
    return TestClient(app)  # no `with`: the app's startup (worker, Redis) is not run for these tests


KEY = {"X-API-Key": get_settings().api_key}  # the key this test run is configured with (tests/conftest.py)


def test_post_creates_and_returns_201(client):
    resp = client.post("/v1/brand-details", json=BODY, headers=KEY)
    assert resp.status_code == 201
    assert resp.json()["brand_id"].startswith("brd_")


def test_get_put_delete_round_trip(client):
    brand_id = client.post("/v1/brand-details", json=BODY, headers=KEY).json()["brand_id"]

    assert client.get(f"/v1/brand-details/{brand_id}", headers=KEY).json()["brand_name"] == "YONO SBI"

    put = client.put(f"/v1/brand-details/{brand_id}", json={**BODY, "brand_name": "YONO"}, headers=KEY)
    assert put.status_code == 200 and put.json()["brand_name"] == "YONO"

    assert client.delete(f"/v1/brand-details/{brand_id}", headers=KEY).status_code == 200
    assert client.get(f"/v1/brand-details/{brand_id}", headers=KEY).status_code == 404


def test_unknown_brand_gives_404_on_read_update_and_delete(client):
    missing = "brd_01HZX0000000000000000000AB"
    assert client.get(f"/v1/brand-details/{missing}", headers=KEY).status_code == 404
    assert client.put(f"/v1/brand-details/{missing}", json=BODY, headers=KEY).status_code == 404
    assert client.delete(f"/v1/brand-details/{missing}", headers=KEY).status_code == 404


def test_same_body_rules_as_discover(client):
    bad = {**BODY, "play_store_url": "https://example.com/app"}
    assert client.post("/v1/brand-details", json=bad, headers=KEY).status_code == 422


def test_missing_key_is_refused(client):
    assert client.post("/v1/brand-details", json=BODY).status_code == 401


# ── scan merge ────────────────────────────────────────────────────────────────

def test_merge_fills_the_request_from_the_stored_brand():
    doc = {
        "brand_id": "brd_01HZX0000000000000000000AB", "brand_name": "YONO SBI", "aliases": ["YONO"],
        "play_store_urls": [PLAY], "app_store_urls": [], "official_domains": ["sbi.co.in"],
    }
    own = [{"store": "play", "id": "com.sbi.lotusintouch", "publisher": "State Bank of India"},
           {"store": "app_store", "id": "com.sbi.YONOBD", "publisher": "State Bank of India"}]
    merged = brand_index.merge_into_request(ScanRequest(brand_id=doc["brand_id"]), doc, own)
    assert merged.brand_name == "YONO SBI"
    assert merged.brand_aliases == ["YONO"]
    assert merged.official_domains == ["sbi.co.in"] and merged.official_domain == "sbi.co.in"
    assert merged.android_packages == ["com.sbi.lotusintouch"]
    assert merged.ios_bundles == ["com.sbi.YONOBD"]
    assert merged.official_publishers == ["State Bank of India"]


def test_merge_keeps_the_values_the_request_gives():
    doc = {"brand_id": "brd_01HZX0000000000000000000AB", "brand_name": "YONO SBI", "aliases": [], "play_store_urls": [],
           "app_store_urls": [], "official_domains": ["sbi.co.in"]}
    merged = brand_index.merge_into_request(
        ScanRequest(brand_id=doc["brand_id"], brand_name="SBI", official_domains=["onlinesbi.sbi"]), doc, [])
    assert merged.brand_name == "SBI"
    assert merged.official_domains == ["onlinesbi.sbi", "sbi.co.in"]


def test_scan_needs_a_brand_name_or_a_brand_id():
    assert ScanRequest(brand_id="brd_01HZX0000000000000000000AB").brand_id
    with pytest.raises(ValidationError):
        ScanRequest()


# ── official apps kept from scan results ──────────────────────────────────────

from app.api.models import AllowlistedItem, ScanReport  # noqa: E402


def _report(*items):
    return ScanReport(scan_id="scn_test", summary={"total_results": 0}, allowlisted=list(items))


def _request():
    return ScanRequest(brand_name="YONO SBI", android_packages=["com.sbi.lotusintouch"], ios_bundles=["com.sbi.YONOBD"])


def test_only_own_store_apps_with_the_brands_id_are_kept():
    report = _report(
        AllowlistedItem(url="u1", source="Google Play", package_id="com.sbi.lotusintouch",
                        app_name="YONO SBI", publisher="State Bank of India", reason="r"),
        AllowlistedItem(url="u2", source="Apple App Store", package_id="com.sbi.YONOBD",
                        app_name="YONO SBI Bangladesh", publisher="State Bank of India", reason="r"),
        # same id on a third-party store: never kept
        AllowlistedItem(url="u3", source="Huawei AppGallery", package_id="com.sbi.lotusintouch",
                        app_name="copy", publisher="someone", reason="r"),
        # on the official store but not the brand's id: never kept
        AllowlistedItem(url="u4", source="Google Play", package_id="com.other.app",
                        app_name="other", publisher="State Bank of India", reason="r"),
        # no publisher: never kept
        AllowlistedItem(url="u5", source="Google Play", package_id="com.sbi.YONOUK",
                        app_name="YONO SBI UK", publisher=None, reason="r"),
    )
    kept = brand_index.official_from_report(report, _request())
    assert [(a["store"], a["id"]) for a in kept] == [("play", "com.sbi.lotusintouch"), ("app_store", "com.sbi.YONOBD")]


async def test_record_allowlist_keeps_each_app_once(es):
    created = await brand_index.create(_details_body())
    app = {"store": "play", "id": "com.sbi.lotusintouch", "name": "YONO", "publisher": "State Bank of India"}
    await brand_index.record_allowlist(created["brand_id"], [app])
    doc = await brand_index.record_allowlist(created["brand_id"], [app])
    assert doc["allowlisted_apps"] == [app]


async def test_update_keeps_the_recorded_apps(es):
    created = await brand_index.create(_details_body())
    app = {"store": "play", "id": "com.a.b", "name": "", "publisher": "P"}
    await brand_index.record_allowlist(created["brand_id"], [app])
    updated = await brand_index.update(created["brand_id"], _details(brand_name="New"))
    assert updated["allowlisted_apps"] == [app]


def test_later_scans_use_the_recorded_apps():
    doc = {
        "brand_id": "brd_01HZX0000000000000000000AB", "brand_name": "YONO SBI", "aliases": [], "play_store_urls": [],
        "app_store_urls": [], "official_domains": [],
        "allowlisted_apps": [{"store": "play", "id": "com.YONOMUApp", "name": "", "publisher": "State Bank of India"}],
    }
    merged = brand_index.merge_into_request(ScanRequest(brand_id=doc["brand_id"]), doc, [])
    assert merged.android_packages == ["com.YONOMUApp"]
    assert merged.official_publishers == ["State Bank of India"]
