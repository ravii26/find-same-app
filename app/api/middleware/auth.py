"""API key authentication middleware."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Security, status
from fastapi.security import APIKeyHeader

from app.config.settings import get_settings

_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(api_key: str | None = Security(_header_scheme)) -> str:
    settings = get_settings()
    if settings.api_key == "change-me-in-production":
        # Open access only when explicitly allowed (local dev). Otherwise a server running with the
        # placeholder key would be an unauthenticated one — fail closed instead of silently open.
        if settings.allow_insecure_defaults:
            return "anonymous"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server misconfigured: API_KEY is the placeholder value",
        )
    if not api_key or not hmac.compare_digest(api_key, settings.api_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header",
        )
    return api_key