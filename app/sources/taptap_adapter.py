"""TapTap adapter — Playwright scraper.

TapTap Global: https://www.taptap.io/search/{query}
Strong in Asia; hosts region-locked games and beta releases. It is a games store, so it only pays off for
brands that ship a game or game-like app.

The site is a JS-rendered SPA. The old `/search?q=` route just renders the home feed (whatever the term),
which is why the adapter used to return nothing; the real results are at `/search/{query}`.

Result: <a href="https://www.taptap.io/app/{numeric id}"> — the icon anchor has no text, the title anchor does.
Auth: None
"""

from __future__ import annotations

import datetime
import hashlib
import logging
import re
from urllib.parse import quote

from app.api.models import (
    FoundVia,
    Hit,
    HitType,
    HostInfo,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.services.keyword_expansion import fold_homoglyphs
from app.sources.base import (
    SourceAdapter,
    is_network_block,
    page_problem,
    pkg_queries,
    says_no_results,
    scrape_status,
)

from app.utils.http_client import throttle

log = logging.getLogger(__name__)

try:
    from playwright.async_api import async_playwright as _async_playwright
    _PLAYWRIGHT_OK = True
except ImportError:
    _async_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_OK = False

_BASE = "https://www.taptap.io"
_SEARCH = _BASE + "/search/{q}"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_APP_URL_RE = re.compile(r"^https://www\.taptap\.io/app/(\d+)/?$")
_APP_LINK_SEL = "a[href*='/app/']"
_EXTRACT_JS = """els => els.map(a => ({
    href: a.href,
    text: (a.innerText || '').trim(),
    icon: (a.querySelector('img') || {}).src || '',
}))"""

_MAX_CARDS = 20


def parse_results(rows: list[dict]) -> list[dict]:
    """Anchor rows -> unique apps (id, name, url). Each app appears several times; the first titled anchor wins.

    Only taptap.io/app/{id} links count (the page also carries store-badge links to apps.apple.com).
    Pure, so it is unit-testable.
    """
    names: dict[str, str] = {}
    icons: dict[str, str | None] = {}
    order: list[str] = []
    for row in rows:
        href = (row.get("href") or "").rstrip("/")
        m = _APP_URL_RE.match(href)
        if not m:
            continue
        app_id = m.group(1)
        if app_id not in names:
            names[app_id] = ""
            icons[app_id] = None
            order.append(app_id)
        lines = [ln.strip() for ln in (row.get("text") or "").splitlines() if ln.strip()]
        if lines and not names[app_id]:
            names[app_id] = lines[0]
        if row.get("icon") and not icons[app_id]:
            icons[app_id] = row["icon"]
    return [
        {"id": i, "name": names[i], "url": f"{_BASE}/app/{i}", "icon_url": icons[i]}
        for i in order if names[i]
    ]


class TapTapAdapter(SourceAdapter):
    name = "taptap"
    method = SourceMethod.scrape
    host_group = "app_store"
    max_results = 10

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:
        if not _PLAYWRIGHT_OK:
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed",
                error="playwright not installed",
            )

        brand = ctx.request.brand_name
        brand_tokens = {t.lower() for t in fold_homoglyphs(brand).split() if len(t) >= 3}

        hits: list[Hit] = []
        seen: set[str] = set()
        errors: list[str] = []
        blocked: list[str] = []

        try:
            async with _async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage",
                          "--disable-gpu", "--no-zygote",
                          "--disable-blink-features=AutomationControlled"],
                )
                bctx = await browser.new_context(
                    accept_downloads=False,
                    user_agent=_UA,
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                )
                await bctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )

                for q in pkg_queries(ctx)[:2]:
                    page = await bctx.new_page()
                    try:
                        try:
                            await throttle(_SEARCH.format(q=quote(q)))
                            resp = await page.goto(
                                _SEARCH.format(q=quote(q)), wait_until="networkidle", timeout=35000,
                            )
                        except Exception as e:
                            (blocked if is_network_block(str(e)) else errors).append(f"{type(e).__name__}: {str(e)[:120]}")
                            if is_network_block(str(e)):
                                break
                            continue

                        status = resp.status if resp else None
                        problem = page_problem(status, await page.title())
                        if problem:
                            blocked.append(f"{problem} from {_BASE}")
                            continue

                        try:
                            await page.wait_for_selector(_APP_LINK_SEL, state="attached", timeout=8000)
                        except Exception:
                            body = await page.inner_text("body")
                            problem = page_problem(status, await page.title(), body)
                            if problem:
                                blocked.append(f"{problem} from {_BASE}")
                            elif not says_no_results(body):
                                errors.append(f"no app links recognised for {q!r} (page layout changed?)")
                            continue

                        rows = await page.eval_on_selector_all(_APP_LINK_SEL, _EXTRACT_JS)
                        for r in parse_results(rows)[:_MAX_CARDS]:
                            name_lower = fold_homoglyphs(r["name"]).lower()
                            if brand_tokens and not any(tok in name_lower for tok in brand_tokens):
                                continue
                            uid = hashlib.sha256(r["url"].encode()).hexdigest()[:32]
                            if uid in seen:
                                continue
                            seen.add(uid)
                            hits.append(Hit(
                                id=uid,
                                type=HitType.app_listing,
                                app_name=r["name"],
                                package_id=None,  # TapTap URLs carry a numeric id, not a package name
                                icon_url=r["icon_url"],
                                url=r["url"],
                                host=HostInfo(
                                    group=self.host_group,
                                    name="TapTap",
                                    domain="taptap.io",
                                    is_official_store=False,
                                ),
                                found_via=[FoundVia(source=self.name, method=self.method, query=q)],
                                relevance_score=0,
                                discovered_at=datetime.datetime.now(datetime.timezone.utc),
                            ))

                    except Exception as e:
                        log.warning("taptap: search failed for query %r: %s", q, e)
                        errors.append(f"{type(e).__name__}: {str(e)[:120]}")
                    finally:
                        await page.close()

                await browser.close()

        except Exception as exc:
            log.warning("taptap: scrape failed: %s", exc)
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed",
                error=f"Playwright error: {exc}",
            )

        return hits, scrape_status(self.name, self.method, hits, errors=errors, blocked=blocked)
