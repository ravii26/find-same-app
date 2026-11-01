# app/api/models.py
"""Pydantic v2 models for the API — request, response, and internal types."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ── Enums ─────────────────────────────────────────────────────────────────────

class ScanStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    completed_with_errors = "completed_with_errors"
    failed = "failed"


class HitType(str, Enum):
    app_listing = "app_listing"
    web_page = "web_page"
    repo = "repo"
    channel_post = "channel_post"


class SourceMethod(str, Enum):
    api = "api"
    scrape = "scrape"


class KeywordType(str, Enum):
    original = "original"
    variant = "variant"
    package = "package"
    lure = "lure"
    typo = "typo"
    homoglyph = "homoglyph"
    phonetic = "phonetic"
    package_guess = "package_guess"


# ── Input schema ──────────────────────────────────────────────────────────────

class ScanCreate(BaseModel):
    """What POST /v1/scans accepts: a brand name. The brand's details are read from its stored record, so any other
    field is refused."""
    model_config = ConfigDict(extra="forbid")

    brand_name: str = Field(..., min_length=1, max_length=200)

    @field_validator("brand_name")
    @classmethod
    def not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("give brand_name")
        return v


class ScanRequest(BaseModel):
    # The brand's name. Its stored record (aliases, keywords, domains, store links) is merged into this request.
    brand_name: str = Field("", max_length=200)
    # Other names the brand goes by, e.g. "YONO" for "YONO SBI". An app name containing one counts as the brand.
    brand_aliases: list[str] = Field(default_factory=list)
    # The brand's own ids. A package id is unique per store, so each id counts only on its platform:
    # android_packages on Android stores, ios_bundles on the App Store (see official_app_ids).
    android_packages: list[str] = Field(default_factory=list)
    ios_bundles: list[str] = Field(default_factory=list)
    official_domain: str | None = Field(None, max_length=253)
    # Allowlist — the brand's own assets, kept out of results (see services/allowlist.py)
    allowlist_packages: list[str] = Field(default_factory=list)   # other legit app IDs, e.g. a business app
    official_publishers: list[str] = Field(default_factory=list)  # developer names on official stores
    official_domains: list[str] = Field(default_factory=list)     # other official registrable domains
    # The brand's store accounts: Google Play developer ids and App Store seller ids (artistId). The store owns
    # these ids, so every app an account publishes on that store is the brand's own (see services/allowlist.py).
    play_developer_ids: list[str] = Field(default_factory=list)
    app_store_seller_ids: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)

    # The body as the client sent it, before a confirmed brand was merged in. Set by the API and reported as the
    # scan's query. Not part of the dedup hash.
    submitted: dict | None = Field(None, exclude=True)

    @model_validator(mode="after")
    def needs_brand_name(self):
        if not self.brand_name.strip():
            raise ValueError("give brand_name")
        return self

    @model_validator(mode="before")
    @classmethod
    def reject_single_ids(cls, data):
        """android_package and ios_bundle were replaced by the lists. Refuse them, so no id is dropped silently."""
        if isinstance(data, dict):
            removed = [k for k in ("android_package", "ios_bundle", "countries") if k in data]
            if removed:
                raise ValueError(f"{', '.join(removed)} was removed: send android_packages or ios_bundles as a list")
        return data

    @property
    def android_package(self) -> str | None:
        """The first Android package id, for code that compares against one id. Read-only: send android_packages."""
        return self.android_packages[0] if self.android_packages else None

    @property
    def ios_bundle(self) -> str | None:
        """The first iOS bundle id, for code that compares against one id. Read-only: send ios_bundles."""
        return self.ios_bundles[0] if self.ios_bundles else None

    def official_app_ids(self) -> dict[str, set[str]]:
        """The brand's own app ids per platform: {"android": {...}, "ios": {...}}, lower-cased."""
        return {
            "android": {p.strip().lower() for p in self.android_packages if p and p.strip()},
            "ios": {p.strip().lower() for p in self.ios_bundles if p and p.strip()},
        }

    def dedup_hash(self) -> str:
        """Stable hash of everything that affects scan results."""
        import hashlib
        import json

        data = self.model_dump(mode="json")
        for key in ("keywords", "allowlist_packages", "official_publishers", "official_domains",
                    "android_packages", "ios_bundles", "brand_aliases", "play_developer_ids", "app_store_seller_ids"):
            data[key] = sorted({str(v).strip().lower() for v in data[key]})
        for key in ("brand_name", "official_domain"):
            if data[key]:
                data[key] = data[key].strip().lower()
        blob = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    @field_validator("allowlist_packages", "official_publishers", "official_domains", "android_packages", "ios_bundles",
                     "brand_aliases", "play_developer_ids", "app_store_seller_ids")
    @classmethod
    def clean_allowlist(cls, v: list[str]) -> list[str]:
        if len(v) > 200:
            raise ValueError("at most 200 entries")
        return list(dict.fromkeys(x.strip() for x in v if x and x.strip()))


# ── Keyword ───────────────────────────────────────────────────────────────────

class Keyword(BaseModel):
    term: str
    type: KeywordType
    reason: str


# ── Hit / Result ──────────────────────────────────────────────────────────────

class FoundVia(BaseModel):
    source: str
    method: SourceMethod
    query: str


class HostInfo(BaseModel):
    group: str
    name: str
    domain: str
    is_official_store: bool = False


class EvidenceRecord(BaseModel):
    """Pointers to captured evidence; paths are relative to the hit's evidence dir."""
    captured_at: datetime
    http_status: int | None = None
    page_path: str | None = None
    page_sha256: str | None = None
    # Where the page actually ended up after redirects, and each hop on the way (for takedown requests)
    final_url: str | None = None
    redirects: list[str] = []
    screenshot_path: str | None = None
    screenshot_sha256: str | None = None
    icon_path: str | None = None
    icon_sha256: str | None = None
    # What the captured page looks like: parked | blank | brand_bait | mentions_brand | neutral
    assessment: str | None = None
    # Why nothing was fetched on purpose, e.g. the link is a file
    skipped: str | None = None
    # Why there is no page.png: screenshots off, Playwright missing, or the browser failed
    screenshot_error: str | None = None
    # Why the page could not be captured: "blocked: ..." (refused, bot protection) or "page fetch failed: ..."
    error: str | None = None


class Hit(BaseModel):
    id: str
    type: HitType
    title: str | None = None
    app_name: str | None = None
    package_id: str | None = None
    version: str | None = None
    publisher: str | None = None
    # The store's id of the publishing account (Google Play developer id, App Store artistId). Set only from
    # Google Play and the App Store themselves, never from a third-party site.
    developer_id: str | None = None
    icon_url: str | None = None
    snippet: str | None = None
    # The store's own description, from the search response we already read (no extra request)
    description: str | None = None
    apk_url: str | None = None
    url: str
    host: HostInfo | None = None
    found_via: list[FoundVia] = Field(default_factory=list)
    relevance_score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    is_official_match: bool = False
    icon_similarity: float | None = None
    permission_similarity: float | None = None
    evidence: EvidenceRecord | None = None
    discovered_at: datetime


# ── Source status ─────────────────────────────────────────────────────────────

class SourceStatus(BaseModel):
    name: str
    method: SourceMethod
    # partial: some queries answered and some failed; the hits found are kept
    status: Literal["ok", "partial", "failed", "blocked", "skipped"]
    results: int = 0
    # Raw count found before the per-source cap was applied (None when uncapped).
    raw_results: int | None = None
    error: str | None = None
    duration_ms: int | None = None


# ── Summary ───────────────────────────────────────────────────────────────────

class ScanSummary(BaseModel):
    total_results: int
    by_source_group: dict[str, int] = Field(default_factory=dict)
    sources_total: int = 0
    sources_ok: int = 0
    sources_failed: int = 0
    sources_skipped: int = 0
    allowlisted_count: int = 0
    suppressed_count: int = 0
    duration_ms: int | None = None
    scoring_ruleset: str = "v1"


# ── Full report ───────────────────────────────────────────────────────────────

class ScanQuery(BaseModel):
    """The scan request as the API received it: every field of the submitted body, passed through unchanged.

    New API fields show up here without changing this class. received_at is not API input, so the scanner sets it.
    """
    model_config = {"extra": "allow"}

    brand_name: str
    received_at: datetime


class AllowlistedItem(BaseModel):
    """A hit suppressed by the allowlist, kept for audit."""
    url: str
    app_name: str | None = None
    package_id: str | None = None
    publisher: str | None = None
    source: str | None = None
    reason: str


class ScanReport(BaseModel):
    scan_id: str
    query: ScanQuery | None = None
    summary: ScanSummary
    results: list[Hit] = Field(default_factory=list)
    allowlisted: list[AllowlistedItem] = Field(default_factory=list)
    # Hits dropped after deeper inspection, with the reason
    suppressed: list[AllowlistedItem] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    expanded_keywords: list[Keyword] = Field(default_factory=list)
    source_statuses: list[SourceStatus] = Field(default_factory=list)
    # Official domains read from the brand's Google Play listing for this scan (not stored on the brand record):
    # [{"domain": "bankofbaroda.com", "from": "developerWebsite"}]
    derived_official_domains: list[dict[str, str]] = Field(default_factory=list)
    # Final scan status (same values as ScanStatus) and, for a failed scan, why it stopped early.
    # A failed report still carries whatever was found before the failure.
    status: str | None = None
    error: str | None = None


# ── Internal context passed to source adapters ────────────────────────────────

class ScanContext(BaseModel):
    scan_id: str
    request: ScanRequest
    expanded_keywords: list[Keyword]
    official_permissions: list[str] = Field(default_factory=list)
    # The brand's own icon from Google Play: the reference for icon comparison, even when the brand's listing is
    # allowlisted and so not among the hits
    official_icon_url: str | None = None

    model_config = {"arbitrary_types_allowed": True}
