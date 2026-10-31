# app/api/routes/brands.py
"""Brand onboarding: discover a brand's apps from one Play link, then the customer confirms the list.

  POST /v1/brands/discover            queue discovery (202), returns brand_id
  GET  /v1/brands/{brand_id}          status, candidates and confirmed ids
  POST /v1/brands/{brand_id}/confirm  save the confirmed candidates; a scan then uses them via brand_id
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator
from ulid import ULID

from app.api.middleware.auth import require_api_key
from app.jobs.actors import discover_brand_actor
from app.services import brand_store
from app.services.discovery import app_store_id_from_url, play_package_from_url

router = APIRouter(prefix="/v1/brands", tags=["brands"])


class DiscoverRequest(BaseModel):
    brand_name: str = Field(..., min_length=1, max_length=200)
    # At least one of the two store links is required. Both may be given.
    play_store_url: str | None = Field(None, max_length=2048)
    app_store_url: str | None = Field(None, max_length=2048)
    official_domains: list[str] = Field(default_factory=list, max_length=20)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    country: str = Field("in", pattern=r"^[A-Za-z]{2}$")

    @field_validator("play_store_url")
    @classmethod
    def must_be_play_link(cls, v: str | None) -> str | None:
        if not v or not v.strip():
            return None
        if not play_package_from_url(v):
            raise ValueError("play_store_url must be a Google Play details link with an id, e.g. "
                             "https://play.google.com/store/apps/details?id=com.example.app")
        return v.strip()

    @field_validator("app_store_url")
    @classmethod
    def must_be_app_store_link(cls, v: str | None) -> str | None:
        if not v or not v.strip():
            return None
        if not app_store_id_from_url(v):
            raise ValueError("app_store_url must be an App Store link with an app id, e.g. "
                             "https://apps.apple.com/in/app/example/id123456789")
        return v.strip()

    @model_validator(mode="before")
    @classmethod
    def sibling_brands_removed(cls, data):
        if isinstance(data, dict) and "sibling_brands" in data:
            raise ValueError("sibling_brands was removed; it is not used")
        return data

    @model_validator(mode="after")
    def needs_one_store_link(self):
        if not self.play_store_url and not self.app_store_url:
            raise ValueError("give play_store_url, app_store_url or both")
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


@router.post("/discover", status_code=status.HTTP_202_ACCEPTED, response_model=None)
async def discover(body: DiscoverRequest, _: Annotated[str, Depends(require_api_key)]):
    brand_id = "brd_" + str(ULID())
    brand_store.create(brand_id, body.model_dump())
    discover_brand_actor.send(brand_id)
    return {"brand_id": brand_id, "status": "queued"}


@router.get("/{brand_id}", response_model=None)
async def get_brand(brand_id: str, _: Annotated[str, Depends(require_api_key)]):
    record = _load_or_404(brand_id)
    return record


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
