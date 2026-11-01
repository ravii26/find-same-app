# app/api/routes/brand_details.py
"""Brand details (Elasticsearch): create, read, update and delete.

  POST   /v1/brand-details           create (201, "Brand added"); 409 if a brand with this name exists
  GET    /v1/brand-details           list every brand, sorted by name (?page=1&size=50, size up to 100)
  GET    /v1/brand-details/{brand}   read by brand name
  PUT    /v1/brand-details/{brand}   replace the details; 409 if the new name is taken
  DELETE /v1/brand-details/{brand}   delete by brand name

Allowlisted apps are not accepted or stored here for now.
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.middleware.auth import require_api_key
from app.services.discovery import app_store_id_from_url, play_package_from_url
from app.storage import brand_index

router = APIRouter(prefix="/v1/brand-details", tags=["brand details"])

_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")
_TERM_MAX = 100  # longest alias or keyword


def _clean_terms(values: list[str], *, label: str) -> list[str]:
    """Trim, collapse spaces, drop blanks and repeats (case-insensitive), and keep the first spelling."""
    cleaned: dict[str, str] = {}
    for value in values:
        term = " ".join((value or "").split())
        if not term:
            continue
        if len(term) > _TERM_MAX:
            raise ValueError(f"{label} '{term[:30]}…' is longer than {_TERM_MAX} characters")
        cleaned.setdefault(term.casefold(), term)
    return list(cleaned.values())


def _clean_domain(value: str) -> str:
    """'https://www.Bank.example/path' -> 'bank.example'. Only the registrable host is kept."""
    host = value.strip().lower()
    host = re.sub(r"^[a-z]+://", "", host).split("/")[0].split("?")[0].split(":")[0].rstrip(".")
    host = host[4:] if host.startswith("www.") else host
    if not _DOMAIN_RE.match(host):
        raise ValueError(f"'{value}' is not a domain, e.g. bankofbaroda.bank.in")
    return host


class BrandDetailsRequest(BaseModel):
    # Any other field is refused, so nothing is dropped silently
    model_config = ConfigDict(extra="forbid")

    brand_name: str = Field(..., min_length=1, max_length=200)
    # One link per app. A brand with apps in several countries sends a link for each package id it publishes;
    # links that share a package id are read once.
    play_store_urls: list[str] = Field(default_factory=list, max_length=20)
    app_store_urls: list[str] = Field(default_factory=list, max_length=20)
    official_domains: list[str] = Field(default_factory=list, max_length=20)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    # Search terms for the sources, besides the brand name (e.g. "login")
    keywords: list[str] = Field(default_factory=list, max_length=50)
    country: str = Field("in", pattern=r"^[A-Za-z]{2}$")

    @model_validator(mode="before")
    @classmethod
    def single_links_join_the_lists(cls, data):
        """play_store_url and app_store_url (one link each) are still accepted and added to the lists."""
        if isinstance(data, dict):
            data = dict(data)
            for single, plural in (("play_store_url", "play_store_urls"), ("app_store_url", "app_store_urls")):
                link = data.pop(single, None)
                if link:
                    data[plural] = [*(data.get(plural) or []), link]
        return data

    @field_validator("brand_name")
    @classmethod
    def clean_name(cls, v: str) -> str:
        name = " ".join(v.split())
        if not re.search(r"[^\W_]", name):
            raise ValueError("brand_name needs at least one letter or digit")
        return name

    @field_validator("play_store_urls")
    @classmethod
    def must_be_play_links(cls, v: list[str]) -> list[str]:
        links = [x.strip() for x in v if x and x.strip()]
        for link in links:
            if not play_package_from_url(link):
                raise ValueError(f"{link} is not a Google Play details link with an id, e.g. "
                                 "https://play.google.com/store/apps/details?id=com.example.app")
        return list(dict.fromkeys(links))

    @field_validator("app_store_urls")
    @classmethod
    def must_be_app_store_links(cls, v: list[str]) -> list[str]:
        links = [x.strip() for x in v if x and x.strip()]
        for link in links:
            if not app_store_id_from_url(link):
                raise ValueError(f"{link} is not an App Store link with an app id, e.g. "
                                 "https://apps.apple.com/in/app/example/id123456789")
        return list(dict.fromkeys(links))

    @field_validator("official_domains")
    @classmethod
    def clean_domains(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(_clean_domain(x) for x in v if x and x.strip()))

    @field_validator("aliases", "keywords")
    @classmethod
    def clean_terms(cls, v: list[str], info) -> list[str]:
        return _clean_terms(v, label=info.field_name)

    @field_validator("country")
    @classmethod
    def lower_country(cls, v: str) -> str:
        return v.lower()

    @model_validator(mode="after")
    def needs_one_link_and_no_self_alias(self):
        if not self.play_store_urls and not self.app_store_urls:
            raise ValueError("give play_store_urls, app_store_urls or both")
        # an alias that is the brand name itself adds nothing
        self.aliases = [a for a in self.aliases if a.casefold() != self.brand_name.casefold()]
        return self


def _http(exc: Exception) -> HTTPException:
    if isinstance(exc, brand_index.BrandNotFound):
        return HTTPException(status_code=404, detail="brand not found")
    if isinstance(exc, brand_index.BrandExists):
        return HTTPException(status_code=409, detail="brand already exists")
    return HTTPException(status_code=503, detail=str(exc))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=None)
async def create_brand_details(body: BrandDetailsRequest, _: Annotated[str, Depends(require_api_key)]):
    try:
        doc = await brand_index.create(body.model_dump())
    except (brand_index.BrandExists, brand_index.BrandIndexUnavailable) as exc:
        raise _http(exc) from exc
    return {"brand_name": doc["brand_name"], "message": "Brand added"}


_MAX_PAGE_SIZE = 100
_MAX_WINDOW = 10_000  # Elasticsearch's default limit on page * size (index.max_result_window)


@router.get("", response_model=None)
async def list_brand_details(
    _: Annotated[str, Depends(require_api_key)],
    page: Annotated[int, Query(ge=1)] = 1,
    size: Annotated[int, Query(ge=1, le=_MAX_PAGE_SIZE)] = 50,
):
    if page * size > _MAX_WINDOW:
        raise HTTPException(status_code=422, detail=f"page * size must be at most {_MAX_WINDOW}")
    try:
        found = await brand_index.list_all(page, size)
    except brand_index.BrandIndexUnavailable as exc:
        raise _http(exc) from exc
    return {"total": found["total"], "page": page, "size": size, "brands": found["brands"]}


@router.get("/{brand}", response_model=None)
async def read_brand_details(brand: str, _: Annotated[str, Depends(require_api_key)]):
    try:
        return await brand_index.read(brand)
    except (brand_index.BrandNotFound, brand_index.BrandIndexUnavailable) as exc:
        raise _http(exc) from exc


@router.put("/{brand}", response_model=None)
async def update_brand_details(brand: str, body: BrandDetailsRequest, _: Annotated[str, Depends(require_api_key)]):
    try:
        return await brand_index.update(brand, body.model_dump())
    except (brand_index.BrandNotFound, brand_index.BrandExists, brand_index.BrandIndexUnavailable) as exc:
        raise _http(exc) from exc


@router.delete("/{brand}", response_model=None)
async def delete_brand_details(brand: str, _: Annotated[str, Depends(require_api_key)]):
    try:
        removed = await brand_index.delete(brand)
    except (brand_index.BrandNotFound, brand_index.BrandIndexUnavailable) as exc:
        raise _http(exc) from exc
    return {"brand_name": removed["brand_name"], "message": "Brand deleted"}
