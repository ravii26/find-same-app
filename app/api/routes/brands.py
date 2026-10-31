# app/api/routes/brands.py
"""Brand confirmation: the customer keeps the discovered apps that are theirs.

  POST /v1/brands/{brand_id}/confirm  save the confirmed candidates; a scan then uses them via brand_id

DiscoverRequest is the body shared with the brand details endpoints (see brand_details.py).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.middleware.auth import require_api_key
from app.services import brand_store
from app.services.discovery import app_store_id_from_url, play_package_from_url

router = APIRouter(prefix="/v1/brands", tags=["brands"])


class DiscoverRequest(BaseModel):
    brand_name: str = Field(..., min_length=1, max_length=200)
    # One link per app. A brand with apps in several countries sends a link for each package id it publishes;
    # links that share a package id are read once.
    play_store_urls: list[str] = Field(default_factory=list, max_length=20)
    app_store_urls: list[str] = Field(default_factory=list, max_length=20)
    official_domains: list[str] = Field(default_factory=list, max_length=20)
    aliases: list[str] = Field(default_factory=list, max_length=20)
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

    @model_validator(mode="before")
    @classmethod
    def sibling_brands_removed(cls, data):
        if isinstance(data, dict) and "sibling_brands" in data:
            raise ValueError("sibling_brands was removed; it is not used")
        return data

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

    @model_validator(mode="after")
    def needs_one_link(self):
        if not self.play_store_urls and not self.app_store_urls:
            raise ValueError("give play_store_urls, app_store_urls or both")
        return self

    @field_validator("official_domains", "aliases")
    @classmethod
    def clean_list(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(x.strip() for x in v if x and x.strip()))


class ConfirmItem(BaseModel):
    store: Literal["play", "app_store"]
    id: str = Field(..., min_length=1, max_length=300)


class ConfirmRequest(BaseModel):
    confirmed_by: str = Field(..., min_length=1, max_length=200)
    selected: list[ConfirmItem] = Field(..., min_length=1, max_length=500)


@router.post("/{brand_id}/confirm", response_model=None)
async def confirm_brand(brand_id: str, body: ConfirmRequest, _: Annotated[str, Depends(require_api_key)]):
    record = _load_or_404(brand_id)
    if record["status"] != "done":
        raise HTTPException(status_code=409, detail=f"discovery is {record['status']}, not ready to confirm")
    try:
        record = brand_store.confirm(record, [s.model_dump() for s in body.selected], body.confirmed_by.strip())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {
        "brand_id": brand_id,
        "status": record["status"],
        "confirmed_at": record["confirmed_at"],
        "confirmed": record["confirmed"],
    }


def _load_or_404(brand_id: str) -> dict:
    record = brand_store.load(brand_id)
    if record is None:
        raise HTTPException(status_code=404, detail="brand not found")
    return record
