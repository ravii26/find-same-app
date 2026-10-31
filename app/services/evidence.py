# app/services/evidence.py
"""Evidence capture for scored hits.

For each selected hit, stores under DATA_DIR/results/{scan_id}/evidence/{hit_id}/:
  - page.png       full-page screenshot (only when CAPTURE_EVIDENCE is enabled)
  - meta.json      URL, final URL and redirects, capture time, HTTP status, SHA-256 of the page and the
                   screenshot, the score and reasons: what a takedown request needs
  - page.html.gz   the raw page, compressed, only for high-risk hits and up to _HTML_CAP_BYTES
  - icon.<type>    the listing's icon, named by the image type found in its bytes

The page check (parked / blank / brand bait) does not depend on this: discovery runs it and keeps the result
on the hit. Only listing pages are fetched, never download URLs. Capture is best-effort: a failure is recorded
on the hit, never raised.
"""

from __future__ import annotations

import asyncio
import datetime
import gzip
import hashlib
import json
import logging
from pathlib import Path

from app.api.models import EvidenceRecord, Hit
from app.config.settings import get_settings
from app.services.page_assessment import assess_page
from app.services.scoring import high_risk_threshold
from app.utils.http_client import FetchError, fetch, is_blocked_message
from app.utils.ssrf_guard import SSRFError, check_url
from app.utils.url_tools import is_download_url

log = logging.getLogger(__name__)

_SCREENSHOT_TIMEOUT_MS = 20_000  # page load
_SCREENSHOT_SECONDS = 60  # whole screenshot, browser start-up included
_HTML_CAP_BYTES = 2 * 1024 * 1024

_IMAGE_TYPES = (
    (b"\x89PNG", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF8", "gif"),
    (b"\x00\x00\x01\x00", "ico"),
)


def evidence_dir(scan_id: str, hit_id: str) -> Path:
    return get_settings().scan_result_dir(scan_id) / "evidence" / hit_id


def _image_type(data: bytes) -> str:
    """File type from the bytes, not the server's content-type, so the icon opens directly."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    for magic, ext in _IMAGE_TYPES:
        if data.startswith(magic):
            return ext
    if data.lstrip()[:5].lower() in (b"<?xml", b"<svg "):
        return "svg"
    return "bin"


def _keep_html(hit: Hit, record: EvidenceRecord, page: bytes) -> bool:
    """The raw page is kept only for the hits that matter in a takedown, and only when it is small enough."""
    risky = hit.relevance_score >= high_risk_threshold() or record.assessment == "brand_bait"
    return risky and len(page) <= _HTML_CAP_BYTES


async def _guard_request(route) -> None:
    """Every request the browser makes, redirects included, goes through the same SSRF check as a fetch."""
    try:
        check_url(route.request.url)
    except SSRFError:
        await route.abort()
    else:
        await route.continue_()


async def _shoot(url: str, dest: Path) -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        # A new browser every time: no profile, cookies or history carry over between pages
        browser = await p.chromium.launch()
        try:
            # Downloads are refused and no permissions are granted: the page's JavaScript runs,
            # but it cannot save files or use the camera, location and so on
            context = await browser.new_context(accept_downloads=False)
            await context.route("**/*", _guard_request)
            page = await context.new_page()
            await page.goto(url, timeout=_SCREENSHOT_TIMEOUT_MS, wait_until="networkidle")
            await page.screenshot(path=str(dest), full_page=True)
        finally:
            await browser.close()


async def _screenshot(url: str, dest: Path) -> str | None:
    """Best-effort full-page screenshot. Returns None when written, otherwise why it was not."""
    try:
        check_url(url)
    except SSRFError as exc:
        log.warning("evidence: screenshot skipped for %s: %s", url, exc)
        return f"not allowed: {exc}"
    try:
        import playwright.async_api  # noqa: F401 — presence check, so a missing install gets its own message
    except ImportError as exc:
        log.warning("evidence: screenshot skipped for %s: %s", url, exc)
        return "playwright is not installed in this image"
    try:
        await asyncio.wait_for(_shoot(url, dest), _SCREENSHOT_SECONDS)
        return None
    except asyncio.TimeoutError:
        log.warning("evidence: screenshot of %s exceeded %ss", url, _SCREENSHOT_SECONDS)
        return f"screenshot exceeded {_SCREENSHOT_SECONDS}s"
    except Exception as exc:
        log.warning("evidence: screenshot failed for %s: %s", url, exc)
        return f"{type(exc).__name__}: {str(exc)[:200]}"


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


async def _capture_page(
    out: Path, hit: Hit, record: EvidenceRecord, brand: str | None, official_domain: str | None,
) -> EvidenceRecord:
    try:
        resp = await fetch(hit.url, rotate_ua=True, retries=1)
    except FetchError as exc:
        # A refusal (bot protection, 403, rate limit) is not the same as the page being down
        kind = "blocked" if is_blocked_message(str(exc)) else "page fetch failed"
        return record.model_copy(update={"error": f"{kind}: {exc}"})
    except Exception as exc:  # never let evidence capture break a scan
        return record.model_copy(update={"error": f"{type(exc).__name__}: {exc}"})

    page = resp.content
    record = record.model_copy(update={
        "http_status": resp.status_code,
        "final_url": str(resp.request.url),
        "redirects": [str(r.url) for r in getattr(resp, "history", [])],
        "page_sha256": hashlib.sha256(page).hexdigest(),
    })
    # The check from discovery is kept; it is only worked out here when discovery did not have it
    if record.assessment is None and brand and hit.host and hit.host.group == "lookalike_domain":
        try:
            record = record.model_copy(update={"assessment": assess_page(page, brand, official_domain)})
        except Exception as exc:  # a parser quirk must never lose the captured evidence
            log.warning("evidence: assessment failed for %s: %s", hit.url, exc)
    if _keep_html(hit, record, page):
        try:
            (out / "page.html.gz").write_bytes(gzip.compress(page))
            record = record.model_copy(update={"page_path": "page.html.gz"})
        except OSError as exc:
            log.warning("evidence: could not write page for %s: %s", hit.id, exc)
    return record


async def _capture_icon(out: Path, url: str, record: EvidenceRecord) -> EvidenceRecord:
    try:
        icon = (await fetch(url, rotate_ua=True, retries=1)).content
    except Exception as exc:
        log.warning("evidence: icon fetch failed for %s: %s", url, exc)
        return record
    name = f"icon.{_image_type(icon)}"
    try:
        (out / name).write_bytes(icon)
    except OSError as exc:
        log.warning("evidence: could not write icon for %s: %s", url, exc)
        return record
    return record.model_copy(update={"icon_path": name, "icon_sha256": hashlib.sha256(icon).hexdigest()})


async def capture_hit(
    scan_id: str, hit: Hit, brand: str | None = None, official_domain: str | None = None,
) -> EvidenceRecord:
    """Save evidence for one hit and return the record to attach to it. Keeps what discovery recorded."""
    captured_at = datetime.datetime.now(datetime.timezone.utc)
    out = evidence_dir(scan_id, hit.id)
    # The probe's check is kept; its "unreachable" note is not, because this capture is a new attempt
    record = (
        hit.evidence.model_copy(update={"captured_at": captured_at, "error": None})
        if hit.evidence else EvidenceRecord(captured_at=captured_at)
    )

    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return record.model_copy(update={"error": f"cannot create evidence dir: {exc}"})

    shot_reason: str | None = None
    if is_download_url(hit.url):
        # A file link is not a page: its APK is analysed separately, so there is nothing to fetch or photograph
        record = record.model_copy(update={"skipped": "direct file link: not fetched as a page"})
        shot_reason = "direct file link: not a page"
    else:
        record = await _capture_page(out, hit, record, brand, official_domain)

    if hit.icon_url:
        record = await _capture_icon(out, hit.icon_url, record)

    if shot_reason is None:
        shot_reason = await _screenshot(hit.url, out / "page.png")
    if shot_reason is None:
        record = record.model_copy(update={
            "screenshot_path": "page.png",
            "screenshot_sha256": _file_sha256(out / "page.png"),
        })
    else:
        record = record.model_copy(update={"screenshot_error": shot_reason})

    meta = {
        "scan_id": scan_id,
        "hit": json.loads(hit.model_dump_json()),
        "evidence": json.loads(record.model_dump_json()),
    }
    try:
        (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("evidence: could not write meta for %s: %s", hit.id, exc)
    return record


async def capture_evidence(
    scan_id: str, hits: list[Hit], brand: str | None = None, official_domain: str | None = None,
) -> list[Hit]:
    """Attach EvidenceRecords to the given hits (concurrency-limited)."""
    sem = asyncio.Semaphore(get_settings().evidence_concurrency)

    async def _one(hit: Hit) -> Hit:
        async with sem:
            return hit.model_copy(update={"evidence": await capture_hit(scan_id, hit, brand, official_domain)})

    return list(await asyncio.gather(*[_one(h) for h in hits]))
