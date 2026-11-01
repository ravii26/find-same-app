"""User reports of rogue apps: target-keyed storage, the request rules and the route.
Elasticsearch is an in-memory stand-in."""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock

import pytest
from elastic_transport import ConnectionError as TransportConnectionError
from elasticsearch import ConflictError, NotFoundError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.routes.reports import ReportCreate
from app.config.settings import get_settings
from app.main import app
from app.storage import brand_index, es_store, report_index

PLAY = "https://play.google.com/store/apps/details?id=com.phonepay.android"
BODY = {
    "brand_name": "PhonePe",
    "url": PLAY,
    "app_name": "PhonePay",
    "package_id": "com.phonepay.android",
    "category": "impersonation",
    "description": "Asked for my UPI PIN after install",
    "country": "IN",
    "seen_at": "2026-10-30T10:00:00Z",
    "reporter": {"reference": "ticket-4411", "contact": "user@example.com"},
    "evidence_urls": ["https://cdn.example.com/shots/4411-1.png"],
}


def _conflict():
    return ConflictError("conflict", meta=MagicMock(status=409), body={})


class _Indices:
    async def exists(self, index):
        return True

    async def create(self, index, mappings):
        pass


class FakeES:
    """The client calls report_index makes, backed by a dict keyed by _id. update() does what the
    _ADD_SUBMISSION script does in Elasticsearch (tested against a real one, not here)."""

    def __init__(self, fail=False, update_conflicts=False):
        self.docs: dict[str, dict] = {}
        self.indices = _Indices()
        self.fail = fail
        self.update_conflicts = update_conflicts
        self.retry_on_conflict = None

    def _check(self):
        if self.fail:
            raise TransportConnectionError("elasticsearch is down")

    async def create(self, index, id, document, refresh=None):
        self._check()
        if id in self.docs:
            raise _conflict()
        self.docs[id] = document

    async def update(self, index, id, script, retry_on_conflict=0, refresh=None, source=False):
        self._check()
        self.retry_on_conflict = retry_on_conflict
        if self.update_conflicts:  # other submissions kept changing it through every retry
            raise _conflict()
        if id not in self.docs:
            raise NotFoundError("not found", meta=MagicMock(status=404), body={})
        s, p = self.docs[id], script["params"]
        for field, value in p["fill"].items():
            if s.get(field) is None and value is not None:
                s[field] = value
        if s.get("category") in (None, "other"):
            s["category"] = p["category"]
        if len(s["submissions"]) < p["max_submissions"]:
            s["submissions"].append(p["submission"])
        s["report_count"] += 1
        s["updated_at"] = p["now"]
        return {"get": {"_source": s}}

    async def close(self):
        pass


@pytest.fixture
def es(monkeypatch):
    client = FakeES()
    monkeypatch.setattr(es_store, "_client", lambda settings: client)
    return client


def _report(**changes):
    body = ReportCreate(**{**BODY, **changes})
    report = {"brand_name": body.brand_name, "url": body.url, "app_name": body.app_name, "package_id": body.package_id,
              "category": body.category, "country": body.country,
              "seen_at": body.seen_at.isoformat() if body.seen_at else None}
    return report, {"description": body.description, "reporter_reference": "ticket-4411",
                    "reporter_contact": "user@example.com", "evidence_urls": body.evidence_urls}


# ── storage ───────────────────────────────────────────────────────────────────

async def test_a_first_report_is_stored_open_with_its_submission(es):
    doc, created = await report_index.submit(*_report())
    assert created
    assert doc["report_id"].startswith("rpt_") and doc["status"] == "open" and doc["report_count"] == 1
    assert es.docs == {"phonepe|play:com.phonepay.android": doc}
    assert doc["submissions"][0]["reporter_reference"] == "ticket-4411"
    assert doc["submissions"][0]["received_at"] == doc["received_at"]


async def test_the_same_play_app_in_another_language_is_the_same_report(es):
    first, _ = await report_index.submit(*_report())
    again, created = await report_index.submit(*_report(url=PLAY + "&hl=en_IN&gl=IN"))
    assert not created
    assert again["report_id"] == first["report_id"]
    assert again["report_count"] == 2 and len(again["submissions"]) == 2
    assert len(es.docs) == 1


async def test_an_app_store_link_is_keyed_by_its_app_id():
    us = report_index.target_key("PhonePe", "https://apps.apple.com/us/app/fake-pe/id123456789")
    gb = report_index.target_key("phonepe", "https://apps.apple.com/gb/app/fake-pe/id123456789?uo=4")
    assert us == gb == "phonepe|app_store:123456789"


async def test_other_links_are_keyed_by_the_normalized_url():
    key = report_index.target_key("PhonePe", "https://Fake-Pe.example/download/")
    assert key == report_index.target_key("PhonePe", "https://fake-pe.example/download?utm_source=x")
    assert key != report_index.target_key("PhonePe", "https://fake-pe.example/other")
    assert key.startswith("phonepe|url:")


async def test_the_same_link_for_another_brand_is_another_report(es):
    await report_index.submit(*_report())
    _, created = await report_index.submit(*_report(brand_name="Paytm"))
    assert created and len(es.docs) == 2


async def test_a_repeat_fills_what_the_report_lacks_and_keeps_what_it_has(es):
    await report_index.submit(*_report(app_name=None, package_id=None, category="other", country=None))
    doc, _ = await report_index.submit(*_report(app_name="Phone Pay", category="scam_bait"))
    assert doc["app_name"] == "Phone Pay" and doc["category"] == "scam_bait" and doc["country"] == "in"
    doc, _ = await report_index.submit(*_report(app_name="Other Name", category="mod"))
    assert doc["app_name"] == "Phone Pay" and doc["category"] == "scam_bait"


async def test_every_submission_is_counted_but_only_the_first_ones_are_kept(es, monkeypatch):
    monkeypatch.setattr(report_index, "MAX_SUBMISSIONS", 2)
    for _ in range(4):
        doc, _ = await report_index.submit(*_report())
    assert doc["report_count"] == 4 and len(doc["submissions"]) == 2


async def test_a_repeat_is_added_inside_elasticsearch_which_retries_on_conflict(es):
    await report_index.submit(*_report())
    await report_index.submit(*_report())
    assert es.retry_on_conflict == report_index._RETRY_ON_CONFLICT >= 20


async def test_a_report_still_changing_after_every_retry_is_unavailable(monkeypatch):
    client = FakeES(update_conflicts=True)
    monkeypatch.setattr(es_store, "_client", lambda settings: client)
    await report_index.submit(*_report())
    with pytest.raises(report_index.ReportIndexUnavailable, match="kept changing"):
        await report_index.submit(*_report())


async def test_unreachable_elasticsearch_is_reported_as_unavailable(monkeypatch):
    monkeypatch.setattr(es_store, "_client", lambda settings: FakeES(fail=True))
    with pytest.raises(report_index.ReportIndexUnavailable):
        await report_index.submit(*_report())


# ── request rules ─────────────────────────────────────────────────────────────

def test_only_brand_and_url_are_required():
    body = ReportCreate(brand_name=" Phone  Pe ", url=PLAY)
    assert body.brand_name == "Phone Pe" and body.category == "other" and body.evidence_urls == []


def test_an_unknown_field_is_refused():
    with pytest.raises(ValidationError):
        ReportCreate(**BODY, severity="high")


@pytest.mark.parametrize("url", ["ftp://fake.example/app.apk", "javascript:alert(1)", "fake.example/app",
                                 "https:///no-host", "https://fake.example/a b", "https://x.example/" + "a" * 2048])
def test_url_must_be_an_http_link(url):
    with pytest.raises(ValidationError):
        ReportCreate(**{**BODY, "url": url})


def test_package_id_must_look_like_an_app_id():
    assert ReportCreate(**{**BODY, "package_id": " net.whatsapp.WhatsApp "}).package_id == "net.whatsapp.WhatsApp"
    with pytest.raises(ValidationError, match="not an app id"):
        ReportCreate(**{**BODY, "package_id": "PhonePay app"})


def test_category_must_be_a_known_one():
    with pytest.raises(ValidationError):
        ReportCreate(**{**BODY, "category": "spam"})


def test_seen_at_in_the_future_is_refused_and_a_time_without_zone_is_utc():
    future = (datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)).isoformat()
    with pytest.raises(ValidationError, match="future"):
        ReportCreate(**{**BODY, "seen_at": future})
    assert ReportCreate(**{**BODY, "seen_at": "2026-10-30T10:00:00"}).seen_at.tzinfo == datetime.UTC


def test_evidence_links_are_web_links_and_at_most_five():
    with pytest.raises(ValidationError):
        ReportCreate(**{**BODY, "evidence_urls": ["file:///etc/passwd"]})
    with pytest.raises(ValidationError):
        ReportCreate(**{**BODY, "evidence_urls": [f"https://cdn.example.com/{i}.png" for i in range(6)]})


def test_blank_text_is_no_value_and_the_country_is_lower_cased():
    body = ReportCreate(**{**BODY, "description": "   ", "app_name": "", "reporter": {"reference": " t-1 "}})
    assert body.description is None and body.app_name is None
    assert body.reporter.reference == "t-1" and body.reporter.contact is None
    assert body.country == "in"


# ── routes ────────────────────────────────────────────────────────────────────

@pytest.fixture
def client(es, monkeypatch):
    async def read(name):
        if brand_index.name_key(name) != "phonepe":
            raise brand_index.BrandNotFound(name)
        return {"brand_name": "PhonePe"}

    monkeypatch.setattr(brand_index, "read", read)
    return TestClient(app)  # no `with`: the app's startup (worker, Redis) is not run for these tests


KEY = {"X-API-Key": get_settings().api_key}  # the key this test run is configured with (tests/conftest.py)


def test_post_says_received_and_a_repeat_says_already_reported(client, es):
    first = client.post("/v1/reports", json=BODY, headers=KEY)
    assert first.status_code == 201
    assert first.json()["status"] == "open" and first.json()["duplicate"] is False
    assert first.json()["message"] == "Report received"

    again = client.post("/v1/reports", json={**BODY, "brand_name": "phonepe"}, headers=KEY)
    assert again.status_code == 200
    assert again.json() == {"report_id": first.json()["report_id"], "status": "open", "duplicate": True,
                            "report_count": 2, "message": "Already reported; this report was added to it"}


def test_the_stored_report_uses_the_stored_brand_spelling(client, es):
    client.post("/v1/reports", json={**BODY, "brand_name": "PHONEPE"}, headers=KEY)
    (doc,) = es.docs.values()
    assert doc["brand_name"] == "PhonePe" and doc["country"] == "in"
    assert doc["submissions"][0]["reporter_contact"] == "user@example.com"


def test_a_report_for_an_unknown_brand_is_not_found(client):
    resp = client.post("/v1/reports", json={**BODY, "brand_name": "Nobody"}, headers=KEY)
    assert resp.status_code == 404 and resp.json()["detail"] == "brand not found"


def test_a_bad_body_is_refused(client):
    assert client.post("/v1/reports", json={**BODY, "url": "not a link"}, headers=KEY).status_code == 422
    assert client.post("/v1/reports", json={"url": PLAY}, headers=KEY).status_code == 422


def test_post_without_the_api_key_is_refused(client):
    assert client.post("/v1/reports", json=BODY).status_code == 401


def test_stores_that_are_down_are_reported_as_unavailable(client, monkeypatch):
    monkeypatch.setattr(es_store, "_client", lambda settings: FakeES(fail=True))
    assert client.post("/v1/reports", json=BODY, headers=KEY).status_code == 503

    async def down(name):
        raise brand_index.BrandIndexUnavailable("brand store unavailable")

    monkeypatch.setattr(brand_index, "read", down)
    assert client.post("/v1/reports", json=BODY, headers=KEY).status_code == 503
