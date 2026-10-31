# app/api/routes/brand_details.py
"""Brand details (Elasticsearch): create, read, update and delete. The body is the same as POST /v1/brands/discover.

  POST   /v1/brand-details              create (201), returns the new brand_id
  GET    /v1/brand-details/{brand_id}   read
  PUT    /v1/brand-details/{brand_id}   replace the details
  DELETE /v1/brand-details/{brand_id}   delete
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.middleware.auth import require_api_key
from app.api.routes.brands import DiscoverRequest
from app.storage import brand_index

router = APIRouter(prefix="/v1/brand-details", tags=["brand details"])


def _errors(exc: Exception) -> HTTPException:
    if isinstance(exc, brand_index.BrandNotFound):
        return HTTPException(status_code=404, detail="brand not found")
    return HTTPException(status_code=503, detail=str(exc))


@router.post("", status_code=status.HTTP_201_CREATED, response_model=None)
async def create_brand_details(body: DiscoverRequest, _: Annotated[str, Depends(require_api_key)]):
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
    brand_id: str, body: DiscoverRequest, _: Annotated[str, Depends(require_api_key)],
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

