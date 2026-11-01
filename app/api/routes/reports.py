# app/api/routes/reports.py
"""Rogue apps reported by users (Elasticsearch).
  POST /v1/reports   store a report: 201 "Report received", or 200 when the same app or page of the brand is already
                     reported (this submission is added to that report)
Reports are stored as sent. Nothing is fetched, verified or scored here: a takedown system reads them later.
"""

from __future__ import annotations

import datetime
import re
from typing import Annotated, Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.middleware.auth import require_api_key
from app.storage import brand_index, report_index

router = APIRouter(prefix="/v1/reports", tags=["reports"])

_URL_MAX = 2048
_EVIDENCE_MAX = 5
# Android package or iOS bundle id: dot-separated parts of letters, digits, "_" and "-"
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)+$")
# A seen_at a little ahead of the server clock is the reporter's clock, not the future
_CLOCK_SKEW = datetime.timedelta(minutes=5)


def _web_link(value: str, label: str) -> str:
    link = value.strip()
    parsed = urlparse(link)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or re.search(r"\s", link):
        raise ValueError(f"{label} must be an http or https link, e.g. https://example.com/app")
    if len(link) > _URL_MAX:
        raise ValueError(f"{label} is longer than {_URL_MAX} characters")
    return link


def _text(value: str | None) -> str | None:
    """Trimmed text with inner spaces kept; a blank value is no value."""
    value = (value or "").strip()
    return value or None


class Reporter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The caller's own id for this report (a ticket or user id)
    reference: str | None = Field(None, max_length=100)
    # How to reach the reporter. Personal data: stored with the report only.
    contact: str | None = Field(None, max_length=200)

    @field_validator("reference", "contact")
    @classmethod
    def trim(cls, v: str | None) -> str | None:
        return _text(v)


class ReportCreate(BaseModel):
    # Any other field is refused, so nothing is dropped silently
    model_config = ConfigDict(extra="forbid")

    brand_name: str = Field(..., min_length=1, max_length=200)
    # The rogue app or page: a store listing, an APK site, a Telegram post, a repository or any web page
    url: str = Field(..., min_length=1)
    app_name: str | None = Field(None, max_length=200)
    package_id: str | None = Field(None, max_length=255)
    category: Literal["impersonation", "scam_bait", "mod", "phishing_site", "other"] = "other"
    description: str | None = Field(None, max_length=2000)
    # Where the reporter saw it
    country: str | None = Field(None, pattern=r"^[A-Za-z]{2}$")
    seen_at: datetime.datetime | None = None
    reporter: Reporter | None = None
    # Links to screenshots or other evidence, hosted by the caller
    evidence_urls: list[str] = Field(default_factory=list, max_length=_EVIDENCE_MAX)

    @field_validator("brand_name")
    @classmethod
    def clean_name(cls, v: str) -> str:
        return " ".join(v.split())

    @field_validator("url")
    @classmethod
    def must_be_a_web_link(cls, v: str) -> str:
        return _web_link(v, "url")

    @field_validator("app_name", "description")
    @classmethod
    def trim(cls, v: str | None) -> str | None:
        return _text(v)

    @field_validator("package_id")
    @classmethod
    def must_look_like_an_app_id(cls, v: str | None) -> str | None:
        v = _text(v)
        if v and not _PACKAGE_RE.match(v):
            raise ValueError(f"'{v}' is not an app id, e.g. com.example.app")
        return v

    @field_validator("country")
    @classmethod
    def lower_country(cls, v: str | None) -> str | None:
        return v.lower() if v else v

    @field_validator("seen_at")
    @classmethod
    def not_in_the_future(cls, v: datetime.datetime | None) -> datetime.datetime | None:
        if v is None:
            return v
        if v.tzinfo is None:
            v = v.replace(tzinfo=datetime.UTC)  # a time without a zone is taken as UTC
        if v > datetime.datetime.now(datetime.UTC) + _CLOCK_SKEW:
            raise ValueError("seen_at is in the future")
        return v

    @field_validator("evidence_urls")
    @classmethod
    def must_be_web_links(cls, v: list[str]) -> list[str]:
        links = [_web_link(x, "evidence_urls") for x in v if x and x.strip()]
        return list(dict.fromkeys(links))


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, brand_index.BrandNotFound):
        return HTTPException(status_code=404, detail="brand not found")
    return HTTPException(status_code=503, detail=str(exc))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=None)
async def create_report(body: ReportCreate, _: Annotated[str, Depends(require_api_key)]):
    """Store a user's report of a rogue app or page. The brand must be stored; it is found by name in any case."""
    try:
        brand = await brand_index.read(body.brand_name)
    except (brand_index.BrandNotFound, brand_index.BrandIndexUnavailable) as exc:
        raise _http(exc) from exc

    report = {
        "brand_name": brand["brand_name"],  # the stored spelling
        "url": body.url,
        "app_name": body.app_name,
        "package_id": body.package_id,
        "category": body.category,
        "country": body.country,
        "seen_at": body.seen_at.isoformat() if body.seen_at else None,
    }
    reporter = body.reporter or Reporter()
    submission = {
        "description": body.description,
        "reporter_reference": reporter.reference,
        "reporter_contact": reporter.contact,
        "evidence_urls": body.evidence_urls,
    }
    try:
        doc, created = await report_index.submit(report, submission)
    except report_index.ReportIndexUnavailable as exc:
        raise _http(exc) from exc

    if created:
        return {"report_id": doc["report_id"], "status": doc["status"], "duplicate": False,
                "received_at": doc["received_at"], "message": "Report received"}
    return JSONResponse(status_code=200, content={
        "report_id": doc["report_id"], "status": doc["status"], "duplicate": True,
        "report_count": doc["report_count"], "message": "Already reported; this report was added to it",
    })
