# app/sources/gitlab_adapter.py
"""GitLab repository search adapter.

Endpoint: https://gitlab.com/api/v4/projects?search={query}
Auth: GITLAB_TOKEN env var (optional — raises limit from 10/min to 2000/min)
Rate limit: 10 req/min unauthenticated, 2000/min authenticated.

Same signal as the GitHub adapter — rogue APKs, fake apps, and fraud tools
distributed via public GitLab repos.
"""

from __future__ import annotations

import datetime
import hashlib
import json as _json
import logging

from app.api.models import (
    FoundVia,
    Hit,
    HitType,
    HostInfo,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.config.settings import get_settings
from app.sources.base import SourceAdapter
from app.utils.http_client import BlockedError, FetchError, fetch

log = logging.getLogger(__name__)

_BASE = "https://gitlab.com/api/v4/projects"


class GitLabAdapter(SourceAdapter):
    name = "gitlab"
    method = SourceMethod.api
    host_group = "code_host"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        settings = get_settings()
        token = settings.gitlab_token

        headers: dict[str, str] = {}
        if token:
            headers["PRIVATE-TOKEN"] = token

        brand = ctx.request.brand_name
        pkg = ctx.request.android_package or ""

        queries: list[str] = [f"{brand} apk"]
        if pkg:
            last = pkg.split(".")[-1]
            if last.lower() != brand.lower():
                queries.append(f"{last} apk")

        hits: list[Hit] = []
        seen: set[str] = set()
        error: str | None = None

        for q in queries[:2]:
            url = (
                f"{_BASE}?search={q}&order_by=last_activity_at"
                f"&sort=desc&per_page=20"
            )
            try:
                resp = await fetch(url, headers=headers, rotate_ua=False)
                items = _json.loads(resp.content)
            except (FetchError, BlockedError) as e:
                error = str(e)
                log.warning("gitlab: request failed for query %r: %s", q, e)
                continue
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                log.warning("gitlab: could not parse response for query %r: %s", q, e)
                continue

            if not isinstance(items, list):
                continue

            for item in items:
                repo_url = item.get("web_url", "")
                if not repo_url:
                    continue
                uid = hashlib.sha256(repo_url.encode()).hexdigest()[:32]
                if uid in seen:
                    continue
                seen.add(uid)

                path_slug = item.get("path") or ""
                repo_name = item.get("name") or ""
                # Repos whose path/name contain "apk" are APK distribution repos
                is_apk_repo = "apk" in path_slug.lower() or "apk" in repo_name.lower()
                group = "apk_site" if is_apk_repo else self.host_group

                hits.append(
                    Hit(
                        id=uid,
                        type=HitType.repo,
                        title=repo_name,                         # full title for display
                        app_name=path_slug or repo_name,         # slug for scoring (shorter → better WRatio)
                        publisher=item.get("namespace", {}).get("path"),
                        url=repo_url,
                        host=HostInfo(
                            group=group,
                            name="GitLab",
                            domain="gitlab.com",
                            is_official_store=False,
                        ),
                        found_via=[
                            FoundVia(source=self.name, method=self.method, query=q)
                        ],
                        relevance_score=0,
                        discovered_at=datetime.datetime.now(datetime.timezone.utc),
                    )
                )

        # Only "failed" when we got nothing AND something errored — a partial
        # result (some queries worked) still counts as ok.
        return hits, SourceStatus(
            name=self.name,
            method=self.method,
            status="failed" if (error and not hits) else "ok",
            results=len(hits),
            error=error,
        )
