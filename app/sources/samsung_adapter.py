"""Samsung Galaxy Store adapter — Playwright scraper.

Samsung's store is a fully JS-rendered React SPA with no public JSON API.
This adapter uses async Playwright to navigate the search page and extract
app listings. The host needs `playwright install chromium`.
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


# Package names embedded in Galaxy Store URLs: /detail/G000XXXXXX or data attrs


class SamsungAdapter(SourceAdapter):
    name = "samsung_galaxy"
    method = SourceMethod.scrape
    host_group = "app_store"
    max_results = 10  # official store — expect 1 official app + a few lookalikes

    async def search(self, ctx: ScanContext) -> tuple[list[Hit], SourceStatus]:

        brand_tokens = {t.lower() for t in ctx.request.brand_name.split() if len(t) >= 3}
        terms = [
            kw.term for kw in ctx.expanded_keywords
            if kw.type.value in ("original", "package")
            and any(tok in kw.term.lower() for tok in brand_tokens)
        ][:3]

        if not terms:
            return [], SourceStatus(name=self.name, method=self.method, status="skipped",
                                    error="no brand-relevant keywords")

        hits: list[Hit] = []
        seen: set[str] = set()
        errors: list[str] = []
        blocked: list[str] = []

        try:
            async with browser_context() as bctx:

                for term in terms:
                    search_url = (
                        f"https://galaxystore.samsung.com/search?q={quote_plus(term)}"
                    )
                    page = await bctx.new_page()
                    try:
                        await throttle(search_url)
                        resp = await page.goto(search_url, wait_until="domcontentloaded", timeout=35000)
                        problem = page_problem(resp.status if resp else None, await page.title())
                        if problem:
                            blocked.append(f"{problem} from galaxystore.samsung.com")
                            continue
                        # Give JS time to render results; networkidle can hang on SPAs
                        try:
                            await page.wait_for_load_state("networkidle", timeout=20000)
                        except Exception:
                            pass  # proceed with whatever rendered

                        content = await page.content()
                        log.debug("samsung: page loaded for %r, content length=%d", term, len(content))

                        # Extract all Samsung app detail links
                        links = await page.query_selector_all("a[href*='/detail/']")
                        for link in links[:20]:
                            href = await link.get_attribute("href") or ""
                            if "/detail/" not in href:
                                continue

                            full_url = (
                                href if href.startswith("http")
                                else f"https://galaxystore.samsung.com{href}"
                            )
                            uid = hashlib.sha256(full_url.encode()).hexdigest()[:32]
                            if uid in seen:
                                continue
                            seen.add(uid)

                            # Try to find the nearest text for app name
                            name = ""
                            try:
                                name_el = await link.query_selector(
                                    "[class*='name'], [class*='title'], span, p"
                                )
                                if name_el:
                                    name = (await name_el.text_content() or "").strip()
                            except Exception as e:
                                log.debug("samsung: failed to read app name for %r: %s", full_url, e)

                            # Package name sometimes embedded in data attrs or page source
                            pkg = embedded_package(href) or embedded_package(content[:8000])

                            hits.append(scraped_hit(
                                self,
                                term,
                                url=full_url,
                                host_name="Samsung Galaxy Store",
                                host_domain="galaxystore.samsung.com",
                                hit_id=uid,
                                title=name or None,
                                app_name=name or None,
                                package_id=pkg or None,
                            ))

                    except Exception as e:
                        log.warning("samsung: failed to load/parse results for term %r: %s", term, e)
                        note_page_error(e, errors, blocked)
                    finally:
                        await page.close()


        except Exception as exc:
            log.warning("samsung: scrape failed: %s", exc)
            return [], SourceStatus(
                name=self.name, method=self.method, status="failed",
                error=f"Playwright error: {exc}",
            )

        # A page that never loaded is not "no results": report it as blocked/failed (or partial with some hits)
        return hits, scrape_status(self.name, self.method, hits, errors=errors, blocked=blocked)
