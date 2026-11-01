"""Brand details: name-keyed storage, the request rules, the routes, and the scan merge. Elasticsearch is an in-memory stand-in."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from elastic_transport import ConnectionError as TransportConnectionError
from elasticsearch import ConflictError, NotFoundError
from fastapi.testclient import TestClient

from app.api.models import ScanRequest
from app.api.routes.brand_details import BrandDetailsRequest
from app.config.settings import get_settings
from app.main import app
from app.storage import brand_index, es_store

PLAY = "https://play.google.com/store/apps/details?id=com.sbi.lotusintouch"
BODY = {
    "brand_name": "YONO SBI",
    "play_store_urls": [PLAY],
    "aliases": ["YONO"],
    "official_domains": ["sbi.co.in"],
    "keywords": ["login"],
    "country": "in",
}


def _not_found():
    return NotFoundError("not found", meta=MagicMock(status=404), body={})


def _conflict():
    return ConflictError("conflict", meta=MagicMock(status=409), body={})


class _Indices:
    async def exists(self, index):
        return True

    async def create(self, index, mappings):
        pass


class FakeES:
    """The client calls brand_index makes, backed by a dict keyed by Elasticsearch _id (the brand name's key)."""

    def __init__(self, fail=False):
        self.docs: dict[str, dict] = {}
        self.indices = _Indices()
        self.fail = fail

    def _check(self):
        if self.fail:
            raise TransportConnectionError("elasticsearch is down")

    async def create(self, index, id, document, refresh=None):
        self._check()
        if id in self.docs:
            raise _conflict()
        self.docs[id] = document

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


def _details_body(**changes):
    return BrandDetailsRequest(**{**BODY, **changes}).model_dump()


# ── storage ───────────────────────────────────────────────────────────────────

async def test_create_stores_under_the_name_and_keeps_keywords(es):
    doc = await brand_index.create(_details_body())
    assert es.docs["yono sbi"]["keywords"] == ["login"]
    assert set(doc) == {*_details_body(), "created_at", "updated_at"}
    assert doc["created_at"] == doc["updated_at"]


async def test_a_second_brand_with_the_same_name_is_refused(es):
    await brand_index.create(_details_body())
    with pytest.raises(brand_index.BrandExists):
        await brand_index.create(_details_body(brand_name="  yono   SBI "))


async def test_read_by_name_in_any_case(es):
    created = await brand_index.create(_details_body())
    assert await brand_index.read("YONO sbi") == created


async def test_read_of_an_unknown_name_is_not_found(es):
    with pytest.raises(brand_index.BrandNotFound):
        await brand_index.read("Nobody")


async def test_update_keeps_created_at(es):
    created = await brand_index.create(_details_body())
    updated = await brand_index.update("YONO SBI", _details_body(aliases=[]))
    assert updated["created_at"] == created["created_at"]
    assert updated["aliases"] == []


async def test_renaming_moves_the_brand_to_the_free_name(es):
    await brand_index.create(_details_body())
    await brand_index.update("YONO SBI", _details_body(brand_name="YONO Group"))
    assert "yono sbi" not in es.docs and "yono group" in es.docs
    assert (await brand_index.read("YONO Group"))["brand_name"] == "YONO Group"


async def test_renaming_onto_a_taken_name_is_refused_and_both_stay(es):
    await brand_index.create(_details_body())
    await brand_index.create(_details_body(brand_name="Other Bank"))
    with pytest.raises(brand_index.BrandExists):
        await brand_index.update("Other Bank", _details_body(brand_name="YONO SBI"))
    assert (await brand_index.read("YONO SBI"))["brand_name"] == "YONO SBI"
    assert (await brand_index.read("Other Bank"))["brand_name"] == "Other Bank"


async def test_update_of_an_unknown_brand_is_not_found(es):
    with pytest.raises(brand_index.BrandNotFound):
        await brand_index.update("Nobody", _details_body())


async def test_delete_by_name_removes_and_returns_the_brand(es):
    await brand_index.create(_details_body())
    removed = await brand_index.delete("YONO SBI")
    assert removed["brand_name"] == "YONO SBI"
    with pytest.raises(brand_index.BrandNotFound):
        await brand_index.read("YONO SBI")
    with pytest.raises(brand_index.BrandNotFound):
        await brand_index.delete("YONO SBI")


async def test_unreachable_elasticsearch_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setattr(es_store, "_client", lambda settings: FakeES(fail=True))
    with pytest.raises(brand_index.BrandIndexUnavailable):
        await brand_index.create(_details_body())


# ── request rules ─────────────────────────────────────────────────────────────

def test_allowlisted_apps_is_refused_not_stored():
    with pytest.raises(ValidationError):
        BrandDetailsRequest(**BODY, allowlisted_apps=[{"store": "play", "id": "com.x.y"}])


def test_brand_id_is_refused_now_that_the_name_is_the_key():
    with pytest.raises(ValidationError):
        BrandDetailsRequest(**BODY, brand_id="brd_01HZX0000000000000000000AB")


def test_domains_are_cleaned_to_the_host():
    req = BrandDetailsRequest(**{**BODY, "official_domains": ["https://www.Bank.co.in/login", "bank.co.in"]})
    assert req.official_domains == ["bank.co.in"]


def test_a_value_that_is_not_a_domain_is_refused():
    with pytest.raises(ValidationError, match="not a domain"):
        BrandDetailsRequest(**{**BODY, "official_domains": ["not a domain"]})


def test_keywords_are_trimmed_and_repeats_dropped_ignoring_case():
    req = BrandDetailsRequest(**{**BODY, "keywords": ["  login ", "LOGIN", "", "kyc  update"]})
    assert req.keywords == ["login", "kyc update"]


def test_keywords_are_optional():
    req = BrandDetailsRequest(**{k: v for k, v in BODY.items() if k != "keywords"})
    assert req.keywords == []


def test_alias_equal_to_the_brand_name_is_dropped():
    req = BrandDetailsRequest(**{**BODY, "aliases": ["yono sbi", "YONO"]})
    assert req.aliases == ["YONO"]


def test_brand_name_is_collapsed_and_needs_a_letter_or_digit():
    assert BrandDetailsRequest(**{**BODY, "brand_name": "  YONO   SBI "}).brand_name == "YONO SBI"
    with pytest.raises(ValidationError, match="letter or digit"):
        BrandDetailsRequest(**{**BODY, "brand_name": "!!!"})


def test_a_brand_needs_one_store_link():
    with pytest.raises(ValidationError, match="play_store_urls, app_store_urls"):
        BrandDetailsRequest(**{**BODY, "play_store_urls": []})


# ── routes ────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(es):
    return TestClient(app)  # no `with`: the app's startup (worker, Redis) is not run for these tests


KEY = {"X-API-Key": get_settings().api_key}  # the key this test run is configured with (tests/conftest.py)


def test_post_says_the_brand_was_added_and_a_repeat_says_it_exists(client):
    first = client.post("/v1/brand-details", json=BODY, headers=KEY)
    assert first.status_code == 201 and first.json() == {"brand_name": "YONO SBI", "message": "Brand added"}
    again = client.post("/v1/brand-details", json={**BODY, "brand_name": "yono  sbi"}, headers=KEY)
    assert again.status_code == 409 and again.json()["detail"] == "brand already exists"


def test_post_with_allowlisted_apps_is_refused(client):
    resp = client.post("/v1/brand-details", json={**BODY, "allowlisted_apps": []}, headers=KEY)
    assert resp.status_code == 422


def test_get_put_delete_by_name(client):
    client.post("/v1/brand-details", json=BODY, headers=KEY)
    assert client.get("/v1/brand-details/YONO SBI", headers=KEY).json()["keywords"] == ["login"]

    put = client.put("/v1/brand-details/YONO SBI", json={**BODY, "brand_name": "YONO"}, headers=KEY)
    assert put.status_code == 200 and put.json()["brand_name"] == "YONO"

    deleted = client.delete("/v1/brand-details/YONO", headers=KEY)
    assert deleted.status_code == 200 and deleted.json() == {"brand_name": "YONO", "message": "Brand deleted"}
    assert client.get("/v1/brand-details/YONO", headers=KEY).status_code == 404


def test_put_onto_a_taken_name_is_409(client):
    client.post("/v1/brand-details", json=BODY, headers=KEY)
    client.post("/v1/brand-details", json={**BODY, "brand_name": "Other"}, headers=KEY)
    assert client.put("/v1/brand-details/Other", json=BODY, headers=KEY).status_code == 409


def test_unknown_brand_gives_404_on_read_update_and_delete(client):
    assert client.get("/v1/brand-details/Nobody", headers=KEY).status_code == 404
    assert client.put("/v1/brand-details/Nobody", json=BODY, headers=KEY).status_code == 404
    assert client.delete("/v1/brand-details/Nobody", headers=KEY).status_code == 404


def test_missing_key_is_refused(client):
    assert client.post("/v1/brand-details", json=BODY).status_code == 401


# ── scan merge ────────────────────────────────────────────────────────────────

def test_merge_fills_the_request_from_the_stored_brand():
    doc = {
        "brand_name": "YONO SBI", "aliases": ["YONO"], "keywords": ["login"],
        "play_store_urls": [PLAY], "app_store_urls": [], "official_domains": ["sbi.co.in"],
    }
    own = [{"store": "play", "id": "com.sbi.lotusintouch", "publisher": "State Bank of India"},
           {"store": "app_store", "id": "com.sbi.YONOBD", "publisher": "State Bank of India"}]
    merged = brand_index.merge_into_request(ScanRequest(brand_name="yono sbi"), doc, own)
    assert merged.brand_name == "YONO SBI"
    assert merged.brand_aliases == ["YONO"]
    assert merged.keywords == ["login"]
    assert merged.official_domains == ["sbi.co.in"] and merged.official_domain == "sbi.co.in"
    assert merged.android_packages == ["com.sbi.lotusintouch"]
    assert merged.ios_bundles == ["com.sbi.YONOBD"]
    assert merged.official_publishers == ["State Bank of India"]


def test_merge_keeps_the_values_the_request_gives():
    doc = {"brand_name": "YONO SBI", "aliases": [], "play_store_urls": [],
           "app_store_urls": [], "official_domains": ["sbi.co.in"]}
    merged = brand_index.merge_into_request(
        ScanRequest(brand_name="YONO SBI", official_domains=["onlinesbi.sbi"]), doc, [])
    assert merged.official_domains == ["onlinesbi.sbi", "sbi.co.in"]


def test_scan_needs_a_brand_name():
    assert ScanRequest(brand_name="YONO SBI").brand_name == "YONO SBI"
    with pytest.raises(ValidationError):
        ScanRequest()
