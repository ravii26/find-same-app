# app/sources/github_adapter.py
"""GitHub Search API adapter.

Endpoint: https://api.github.com/search/repositories
Auth: Authorization: Bearer <token> (GITHUB_TOKEN env var; optional)
Rate limits: Unauth: 10/min search, 60/hr general. Auth: 30/min search, 5000/hr.
"""

from __future__ import annotations

import hashlib
import json as _json
import logging

from app.api.models import (
    Hit,
    HitType,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.config.settings import get_settings
from app.sources.base import SourceAdapter, scraped_hit
from app.utils.http_client import BlockedError, FetchError, fetch

log = logging.getLogger(__name__)


class GitHubAdapter(SourceAdapter):
    name = "github"
    method = SourceMethod.api
    host_group = "code_host"

    _BASE = "https://api.github.com/search/repositories"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        settings = get_settings()
        token = settings.github_token

        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        brand = ctx.request.brand_name
        pkg = ctx.request.android_package or ""
        queries = [brand]
        if pkg:
            queries.append(pkg.split(".")[-1])

        hits: list[Hit] = []
        seen: set[str] = set()
        error: str | None = None

        for q in queries[:2]:
            url = f"{self._BASE}?q={q}+apk&sort=updated&per_page=30"
            try:
                resp = await fetch(url, headers=headers, rotate_ua=False)
                data = _json.loads(resp.content)
            except (FetchError, BlockedError) as e:
                error = str(e)
                log.warning("github: request failed for query %r: %s", q, e)
                continue
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                log.warning("github: could not parse response for query %r: %s", q, e)
                continue

            for item in data.get("items", []):
                repo_url = item.get("html_url", "")
                if not repo_url:
                    continue
                uid = hashlib.sha256(repo_url.encode()).hexdigest()[:32]
                if uid in seen:
                    continue
                seen.add(uid)
                repo_name = item.get("name") or ""
                full_name = item.get("full_name") or ""
                hits.append(
                    scraped_hit(
                        self,
                        q,
                        url=repo_url,
                        host_name="GitHub",
                        host_domain="github.com",
                        group=self.host_group,
                        hit_id=uid,
                        hit_type=HitType.repo,
                        title=full_name,
                        app_name=repo_name,
                        publisher=item.get("owner", {}).get("login"),
                    )
                )

        return hits, SourceStatus(
            name=self.name,
            method=self.method,
            status="failed" if (error and not hits) else "ok",
            results=len(hits),
            error=error,
        )

