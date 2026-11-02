
# app/sources/uptodown_adapter.py
"""Uptodown adapter — Playwright Android app store scraper.

Uptodown does not return useful data via static HTTP (JS-rendered).
This adapter uses async Playwright; the host needs `playwright install chromium`.
"""

from __future__ import annotations

import hashlib
import logging
from urllib.parse import quote_plus

from app.api.models import (
    Hit,
    ScanContext,
    SourceMethod,
    SourceStatus,
)
from app.services.brand_match import presence_any
from app.sources.base import (
    SourceAdapter,
    embedded_package,
    note_page_error,
    page_problem,
    scrape_status,
    scraped_hit,
)
from app.utils.browser import browser_context
from app.utils.http_client import throttle

log = logging.getLogger(__name__)



class UptodownAdapter(SourceAdapter):
    name = "uptodown"
    method = SourceMethod.scrape
    host_group = "apk_site"

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:

        brand_tokens = {t.lower() for t in ctx.request.brand_name.split() if len(t) >= 3}
        brands = [ctx.request.brand_name, *ctx.request.brand_aliases]
        terms = [
            kw.term for kw in ctx.expanded_keywords
            if kw.type.value in ("original", "lure")
            and any(tok in kw.term.lower() for tok in brand_tokens)
        ][:5]

        hits: list[Hit] = []
        seen_pkgs: set[str] = set()
        errors: list[str] = []
        blocked: list[str] = []

        try:
            async with browser_context() as bctx:

                for term in terms:
                    search_url = f"https://en.uptodown.com/android/search?query={quote_plus(term)}"

                    app_urls: list[str] = []
                    page = await bctx.new_page()
                    try:
                        await throttle(search_url)
                        resp = await page.goto(search_url, wait_until="networkidle", timeout=30000)
                        problem = page_problem(resp.status if resp else None, await page.title())
                        if problem:
                            blocked.append(f"{problem} from en.uptodown.com")
                            continue
                        for a in await page.query_selector_all("a[href*='.en.uptodown.com/android']"):
                            href = await a.get_attribute("href") or ""
                            if href and href not in app_urls:
                                app_urls.append(href)
                    except Exception as e:
                        log.warning("uptodown: search page parse failed for term %r: %s", term, e)
                        note_page_error(e, errors, blocked)
                    finally:
                        await page.close()

                    for app_url in app_urls[:8]:
                        app_page = await bctx.new_page()
                        try:
                            await throttle(app_url)
                            await app_page.goto(app_url, wait_until="domcontentloaded", timeout=20000)

                            # Try explicit package table cell first, then regex fallback
                            pkg = ""
                            pkg_el = await app_page.query_selector(
                                "th:text('Package Name') + td, th:text('Package name') + td"
                            )
                            if pkg_el:
                                pkg = (await pkg_el.text_content() or "").strip()
                            if not pkg:
                                pkg = embedded_package(await app_page.content())
                            if not pkg or pkg in seen_pkgs:
                                continue
                            seen_pkgs.add(pkg)

                            name = ""
                            name_el = await app_page.query_selector("#detail-app-name, h1")
                            if name_el:
                                name = (await name_el.text_content() or "").strip()

                            # Uptodown's search returns any "phone" app: keep only the ones named or packaged as the brand
                            if presence_any(brands, name, pkg, None) is None:
                                continue  # not the brand, nor a near-miss spelling of it

                            dev = ""
                            dev_el = await app_page.query_selector("#author-link, .autor button, .autor a")
                            if dev_el:
                                dev = (await dev_el.text_content() or "").strip()

                            icon_url: str | None = None
                            icon_el = await app_page.query_selector("img[itemprop='image']")
                            if icon_el:
                                icon_url = await icon_el.get_attribute("src") or None

                            version: str | None = None
                            version_el = await app_page.query_selector(".version")
                            if version_el:
                                version = (await version_el.text_content() or "").strip() or None

                            description: str | None = None
                            desc_el = await app_page.query_selector(".text-description")
                            if desc_el:
                                description = (await desc_el.text_content() or "").strip()[:1000] or None  # same cap

                            uid = hashlib.sha256(app_url.encode()).hexdigest()[:32]
                            hits.append(scraped_hit(
                                self,
                                term,
                                url=app_url,
                                host_name="Uptodown",
                                host_domain="uptodown.com",
                                hit_id=uid,
                                title=name or pkg,
                                app_name=name or None,
                                package_id=pkg,
                                publisher=dev or None,
                                icon_url=icon_url,
                                version=version,
                                description=description,
                            ))
                        except Exception as e:
                            log.warning("uptodown: app page parse failed for %r: %s", app_url, e)
                            note_page_error(e, errors, blocked)
                        finally:
                            await app_page.close()


        except Exception as exc:
            log.warning("uptodown: scrape failed: %s", exc)
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed",
                error=f"Playwright error: {exc}",
            )

        # A page that never loaded is not "no results": report it as blocked/failed (or partial with some hits)
        return hits, scrape_status(self.name, self.method, hits, errors=errors, blocked=blocked)
