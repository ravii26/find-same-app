# app/api/routes/brand_details.py
"""Brand details (Elasticsearch): create, read, update and delete.

  POST   /v1/brand-details              create (201), returns the new brand_id
  GET    /v1/brand-details/{brand_id}   read
  PUT    /v1/brand-details/{brand_id}   replace the details
  DELETE /v1/brand-details/{brand_id}   delete
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.middleware.auth import require_api_key
from app.services.discovery import app_store_id_from_url, play_package_from_url
from app.storage import brand_index

router = APIRouter(prefix="/v1/brand-details", tags=["brand details"])


class BrandDetailsRequest(BaseModel):
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


def _errors(exc: Exception) -> HTTPException:
    if isinstance(exc, brand_index.BrandNotFound):
        return HTTPException(status_code=404, detail="brand not found")
    return HTTPException(status_code=503, detail=str(exc))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=None)
async def create_brand_details(body: BrandDetailsRequest, _: Annotated[str, Depends(require_api_key)]):
    try:
        return await brand_index.create(body.model_dump())
    except brand_index.BrandIndexUnavailable as exc:
        raise _errors(exc) from exc


@router.get("/{brand_id}", response_model=None)
async def read_brand_details(brand_id: str, _: Annotated[str, Depends(require_api_key)]):
    try:
        return await brand_index.read(brand_id)
    except (brand_index.BrandNotFound, brand_index.BrandIndexUnavailable) as exc:
        raise _errors(exc) from exc


@router.put("/{brand_id}", response_model=None)
async def update_brand_details(
    brand_id: str, body: BrandDetailsRequest, _: Annotated[str, Depends(require_api_key)],
):
    try:
        return await brand_index.update(brand_id, body.model_dump())
    except (brand_index.BrandNotFound, brand_index.BrandIndexUnavailable) as exc:
        raise _errors(exc) from exc


@router.delete("/{brand_id}", response_model=None)
async def delete_brand_details(brand_id: str, _: Annotated[str, Depends(require_api_key)]):
    try:
        await brand_index.delete(brand_id)
    except (brand_index.BrandNotFound, brand_index.BrandIndexUnavailable) as exc:
        raise _errors(exc) from exc
    return {"brand_id": brand_id, "message": "Brand deleted"}

