# app/services/scoring.py
"""
Relevance scoring for discovered hits.

Scoring is deterministic given the same YAML weights and input.
CPU-heavy fuzzy matching runs in a thread pool to keep the event loop free.
pHash icon comparison runs as an async post-scoring enrichment step.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz import fuzz

from app.api.models import Hit, ScanRequest
from app.config.phrases import word_regex
from app.services.brand_match import exact_in, presence, presence_any
from app.services.keyword_expansion import fold_homoglyphs
from app.utils.url_tools import decode_idn, registrable_domain

log = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "scoring.yaml"
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="scorer")

_config: dict[str, Any] | None = None


def _load_config() -> dict[str, Any]:
    global _config
    if _config is None:
        _config = yaml.safe_load(_CONFIG_PATH.read_text())
    return _config


def _fuzzy(a: str, b: str) -> float:
    """WRatio on homoglyph-folded, lowercased strings. Returns 0–1."""
    return fuzz.WRatio(fold_homoglyphs(a), fold_homoglyphs(b)) / 100.0


# Word lists come from config/phrases.yaml (all markets/languages: a listing can be in any language).
_LURE_RE = word_regex("lure_words")


# Vocabulary of banking/payment scams: the file or page is named for the *lure*, not just the brand.
_SCAM_RE = word_regex("scam_words")

# Shared hosting platforms: a brand name in a *subdomain* here is not the brand's own site.
_FREE_HOSTING = (
    "web.app", "firebaseapp.com", "herokuapp.com", "pages.dev", "github.io", "netlify.app", "vercel.app",
    "workers.dev", "glitch.me", "blogspot.com", "wixsite.com", "weebly.com", "000webhostapp.com",
    "onrender.com", "repl.co", "surge.sh", "trycloudflare.com", "ngrok.io", "ngrok-free.app",
)


def _free_hosting_subdomain(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for suffix in _FREE_HOSTING:
        if host.endswith("." + suffix):
            return host[: -(len(suffix) + 1)]
    return None


# Utility/companion apps ("Status Saver for WhatsApp", "Stickers") use the brand to say what they
# work with. Common and mostly harmless, so they rank below impersonation — unless the name also
# carries distribution/mod wording ("WhatsApp Plus", "Beta ... Updates"), which is the risky kind.
_COMPANION_WORDS = re.compile(
    r"\b(stickers?|wastickerapps?|status|wallpapers?|dps?|quotes|images?|emoji|sim|cleaner|tools?|"
    r"keyboard|scanner|widget|swab|notes?|guide|tips|chat bot|autoreply|auto reply)\b", re.IGNORECASE)
_RISKY_NAME_WORDS = re.compile(
    r"\b(mod|plus|gb|pro|lite|premium|hack|cheats?|cracked|unlocked|apk|latest|updates?|upgrade|official|"
    r"beta|version|real|new|vip|gold|rewards?|kyc|bonus|cash|login|free)\b", re.IGNORECASE)
_FOR_BRAND = re.compile(r"\bfor\s+(?:the\s+)?(\S+(?:\s+\S+)?)", re.IGNORECASE)


def is_companion_app(name: str, brand: str) -> bool:
    if not name or _RISKY_NAME_WORDS.search(name) or not exact_in(brand, name):
        return False
    m = _FOR_BRAND.search(name)
    if m and exact_in(brand, m.group(1)):
        return True
    return bool(_COMPANION_WORDS.search(name))


def _score_one(
    hit_title: str | None,
    hit_app_name: str | None,
    hit_package_id: str | None,
    hit_publisher: str | None,
    hit_url: str,
    hit_host_group: str | None,
    brand_name: str,
    android_package: str | None,
    official_domain: str | None,
    is_official_store: bool = False,
    is_official_match: bool = False,
    publisher_is_official: bool = False,
    hit_snippet: str | None = None,
    brand_aliases: tuple[str, ...] = (),
    brand_packages: tuple[str, ...] = (),
) -> tuple[int, list[str]]:
    cfg = _load_config()
    weights = cfg["weights"]
    signals = cfg["signals"]

    score = 0.0
    reasons: list[str] = []

    # Name similarity
    name = hit_app_name or hit_title or ""
    if name:
        sim = _fuzzy(name, brand_name)
        score += sim * weights["name_similarity"] * 100
        # Near-exact name on an official store from a non-official listing is the
        # classic impersonation pattern; name similarity alone caps below the
        # relevance threshold, so it needs its own signal.
        if is_official_store and not is_official_match and sim >= 0.9:
            score += signals.get("official_store_name_clone", 15)
            reasons.append("near-exact brand name on an official store, not the official listing")
        if sim > 0.9:
            reasons.append(f"app name '{name}' is very similar to brand name")
        elif sim > 0.7:
            reasons.append(f"app name '{name}' resembles brand name")

    # Package ID similarity against every official Android id, so each package of a multi-app brand counts
    official_pkgs = brand_packages or ((android_package,) if android_package else ())
    # Package ids are compared without case: mirrors often list them lower-cased
    is_official_pkg = (hit_package_id or "").lower() in {p.lower() for p in official_pkgs}
    if hit_package_id and official_pkgs:
        pkg_sim = max(_fuzzy(hit_package_id, p) for p in official_pkgs)
        score += pkg_sim * weights["package_similarity"] * 100
        if pkg_sim > 0.85:
            reasons.append(f"package ID '{hit_package_id}' closely matches official package")
        if not is_official_pkg and pkg_sim > 0.7:
            score += signals["package_id_suffix_variation"]
            reasons.append("package ID has suffix variation of official ID")

    # Domain similarity — rapidfuzz + TLD checks
    official_reg = registrable_domain(official_domain) if official_domain else None
    if official_reg:
        hit_reg = registrable_domain(hit_url)
        hit_decoded = decode_idn(hit_reg)
        official_decoded = decode_idn(official_reg)
        if hit_decoded and official_decoded and hit_decoded != official_decoded:
            dom_sim = _fuzzy(hit_decoded, official_decoded)
            score += dom_sim * weights["domain_similarity"] * 100
            if dom_sim > 0.85:
                reasons.append(
                    f"domain '{hit_decoded}' is very similar to official '{official_decoded}'"
                )
            # TLD swap
            h_parts = hit_decoded.rsplit(".", 1)
            o_parts = official_decoded.rsplit(".", 1)
            if len(h_parts) == 2 and len(o_parts) == 2:
                if h_parts[0] == o_parts[0] and h_parts[1] != o_parts[1]:
                    score += signals["tld_swap"]
                    reasons.append(f"TLD swapped from '.{o_parts[1]}' to '.{h_parts[1]}'")
            # Brand + keyword pattern
            if official_reg.split(".")[0] in hit_decoded:
                score += signals["domain_has_brand_plus_keyword"]
                reasons.append("domain contains brand name with added keywords")

        # Jaro-Winkler domain lookalike (catches homoglyph/subtle substitution attacks)
        if official_domain:
            try:
                from app.utils.similarity import domain_lookalike
                jw_sim = domain_lookalike(hit_url, [official_domain])
                if jw_sim >= 0.85:
                    score += jw_sim * signals.get("domain_jw_lookalike", 12)
                    reasons.append(
                        f"domain label is a JW lookalike of official (similarity {jw_sim:.2f})"
                    )
            except Exception as exc:
                log.warning("domain lookalike check failed for %s: %s: %s", hit_url, type(exc).__name__, exc)

    # Publisher resembling the brand is a rogue signal only when the publisher is NOT the official one:
    # an impersonator calls itself "PhonePe Official Team"; the genuine publisher is simply the owner.
    if hit_publisher and not publisher_is_official:
        pub_sim = _fuzzy(hit_publisher, brand_name)
        score += pub_sim * weights["publisher_match"] * 100

    # APK site host
    if hit_host_group in ("apk_site", "file_host"):
        score += weights["apk_site_host"] * 100
        reasons.append(f"listed on {hit_host_group.replace('_', ' ')}")

    # Extend keyword checks into the search snippet (DDG/Bing description text)
    search_text = name + (" " + hit_snippet if hit_snippet else "")

    # Lure keyword in title or description
    matching_lures = sorted({m.lower() for m in _LURE_RE.findall(search_text)})
    if matching_lures:
        score += signals["lure_keyword_in_title"]
        label = "title or description" if hit_snippet else "title"
        reasons.append(f"{label} contains lure keyword(s): {', '.join(matching_lures)}")

    match = presence_any([brand_name, *brand_aliases], name, hit_package_id, hit_publisher)

    # Scam-lure vocabulary next to a brand match ("SBI rewards", "hdfc-kyc-update").
    scam = sorted({m.lower() for m in _SCAM_RE.findall(search_text)})
    if scam and match in ("exact", "fuzzy"):
        score += signals.get("scam_lure_keyword", 6)
        reasons.append(f"brand name combined with scam-lure word(s): {', '.join(scam)}")

    # Brand name in a subdomain of a free hosting platform.
    sub = _free_hosting_subdomain(hit_url)
    if sub and presence(brand_name, sub, None, None) in ("exact", "fuzzy"):
        score += signals.get("brand_on_free_hosting", 10)
        reasons.append("brand name used in a subdomain of a free hosting platform")

    # Companion/utility app for the brand: not an impersonation attempt
    if (
        match == "exact" and not is_official_match
        and not is_official_pkg
        and is_companion_app(name, brand_name)
    ):
        score -= signals.get("companion_app_penalty", 15)
        reasons.append("looks like a companion/utility app for the brand, not an impersonation")

    # Brand-presence caps. No brand match at all: generic apps that merely share a word
    # can't score high. Only an approximate (lookalike-spelling) match: ranked below
    # exact matches unless other evidence (icon, cert, permissions) lifts it.
    if match is None:
        cap = signals.get("no_brand_presence_cap", 34)
        if score > cap:
            score = cap
            reasons.append("brand name absent from title/package/publisher — score capped")
    elif match in ("fuzzy", "fuzzy_weak"):
        weak = match == "fuzzy_weak"
        reasons.append(
            "name/package is loosely spelled like the brand (weak lookalike signal)" if weak
            else "name/package/publisher is spelled like the brand (possible lookalike)"
        )
        cap = signals.get("fuzzy_weak_brand_cap", 44) if weak else signals.get("fuzzy_brand_cap", 59)
        if score > cap:
            score = cap

    final = max(0, min(100, round(score)))
    if not reasons:
        # A score nobody can explain gets ignored: say what it is made of.
        parts = []
        if name:
            parts.append(f"name similarity {_fuzzy(name, brand_name):.0%}")
        if hit_package_id and android_package:
            parts.append(f"package similarity {_fuzzy(hit_package_id, android_package):.0%}")
        if hit_host_group:
            parts.append(f"host group {hit_host_group.replace('_', ' ')}")
        reasons.append("weak match: " + (", ".join(parts) if parts else "no individual signal"))
    return final, reasons


_PAGE_MARK = "[page]"


def apply_page_assessment(hit: Hit) -> Hit:
    """Re-rank a lookalike domain by what its page serves. Idempotent.

    Only a page that is brand bait or mentions the brand keeps a lookalike domain in the report; a parked,
    blank, neutral or unreachable one is pushed under the reporting threshold (it stays in the audit list).
    """
    if any(_PAGE_MARK in r for r in hit.reasons):
        return hit
    kind = hit.evidence.assessment if hit.evidence else None
    is_lookalike = bool(hit.host and hit.host.group == "lookalike_domain")
    sig = _load_config()["signals"]
    score, note = hit.relevance_score, None
    if kind == "brand_bait":
        score = min(100, score + sig.get("brand_bait_page", 15))
        note = f"{_PAGE_MARK} page repeats the brand with lure wording and never links to the real site"
    elif kind == "mentions_brand":
        note = f"{_PAGE_MARK} page mentions the brand"
    elif is_lookalike or kind in ("parked", "blank"):
        score = min(score, sig.get("unsuspicious_page_cap", 34))
        served = {
            "parked": "page is parked",
            "blank": "page is blank",
            "neutral": "page does not mention the brand",
        }.get(kind or "", "page could not be checked (unreachable or not probed)")
        note = f"{_PAGE_MARK} {served} — domain exists, nothing suspicious served"
    if note is None:
        return hit
    return hit.model_copy(update={"relevance_score": score, "reasons": hit.reasons + [note]})


async def score_hit(hit: Hit, request: ScanRequest, official_publishers: set[str] | frozenset[str] = frozenset()) -> Hit:
    """Score a single Hit in a thread pool and return the enriched Hit."""
    from app.services.allowlist import _norm_publisher

    publisher_is_official = bool(hit.publisher and _norm_publisher(hit.publisher) in official_publishers)
    loop = asyncio.get_event_loop()
    score, reasons = await loop.run_in_executor(
        _executor,
        _score_one,
        hit.title,
        hit.app_name,
        hit.package_id,
        hit.publisher,
        hit.url,
        hit.host.group if hit.host else None,
        request.brand_name,
        request.android_package,
        request.official_domain,
        bool(hit.host and hit.host.is_official_store),
        hit.is_official_match,
        publisher_is_official,
        hit.snippet,
        tuple(request.brand_aliases),
        tuple(request.android_packages),
    )
    return hit.model_copy(update={"relevance_score": score, "reasons": reasons})


async def _enrich_phash(hits: list[Hit], brand_name: str = "", icon_cache: dict[str, str] | None = None) -> list[Hit]:
    """Add icon_similarity to non-official hits by comparing pHash against the
    official app's icon.  Only runs when an official hit with icon_url exists."""
    try:
        from app.utils.phash import compute_phash, phash_similarity
    except ImportError:
        return hits

    official = next((h for h in hits if h.is_official_match and h.icon_url), None)
    if not official:
        return hits

    official_hash = await compute_phash(official.icon_url, cache=icon_cache)  # type: ignore[arg-type]
    if not official_hash:
        return hits

    cfg = _load_config()
    signals = cfg["signals"]

    async def _enrich_one(hit: Hit) -> Hit:
        if hit.is_official_match or not hit.icon_url:
            return hit
        candidate_hash = await compute_phash(hit.icon_url, cache=icon_cache)
        if not candidate_hash:
            return hit
        sim = phash_similarity(official_hash, candidate_hash)
        updates: dict = {"icon_similarity": round(sim, 3)}
        if brand_name:
            brand_absent = presence(brand_name, hit.app_name, hit.package_id, hit.publisher) is None
        else:
            brand_absent = any("brand name absent" in r for r in hit.reasons)
        if sim >= 0.95:
            # Near-pixel-perfect clone — strong enough signal even without brand tokens
            # (attacker may intentionally use a different name to avoid detection)
            bonus = signals.get("icon_phash_identical", 20)
            updates["relevance_score"] = min(100, hit.relevance_score + bonus)
            updates["reasons"] = hit.reasons + [
                f"icon is nearly identical to official app (pHash similarity {sim:.2f})"
            ]
        elif sim >= 0.80 and not brand_absent:
            # Softer signal — only boost brand-present hits to avoid coincidental matches
            bonus = signals.get("icon_phash_similar", 10)
            updates["relevance_score"] = min(100, hit.relevance_score + bonus)
            updates["reasons"] = hit.reasons + [
                f"icon resembles official app (pHash similarity {sim:.2f})"
            ]
        elif sim >= 0.70:
            # Moderate similarity — flag for analyst review but don't score-boost
            updates["reasons"] = hit.reasons + [
                f"icon moderately resembles official app (pHash similarity {sim:.2f})"
            ]
        return hit.model_copy(update=updates)

    enriched = await asyncio.gather(*[_enrich_one(h) for h in hits])
    return list(enriched)


async def _enrich_permission_similarity(
    hits: list[Hit],
    official_permissions: list[str],
    brand_name: str = "",
    official_package: str | None = None,
) -> list[Hit]:
    """Fetch candidate permissions from Play Store and apply two signals:
      1. Excess dangerous permissions — rogue requests dangerous perms the official doesn't
      2. Permission Jaccard — overall overlap with the official app's permissions

    Only runs when official_permissions is non-empty.
    Uses google_play_scraper.permissions() — no APK download required.
    """
    if not official_permissions:
        return hits

    from app.services.play_listing import (
        android_excess_dangerous as excess_dangerous,
        android_permission_jaccard as permission_jaccard,
        fetch_permissions,
    )

    cfg = _load_config()
    signals = cfg["signals"]

    async def _enrich_one(hit: Hit) -> Hit:
        if hit.is_official_match or not hit.package_id:
            return hit

        # The official package itself (a copy on a mirror) has the official permissions by definition:
        # identical permissions are no evidence against it.
        if official_package and hit.package_id.lower() == official_package.lower():
            return hit

        # Skip permission bonuses for any brand-absent hit.
        # The score-cap reason is only added when raw score > 35, so hits that
        # naturally score below 35 (but could be pushed above by permission
        # bonuses) are not caught by a reason-string check — re-derive directly.
        if brand_name and presence(brand_name, hit.app_name, hit.package_id, hit.publisher) is None:
            return hit

        # Only enrich hits where we have an exact package ID to look up on Play Store
        if not (hit.host and hit.host.domain in (
            "play.google.com", "aptoide.com", "global.app.mi.com",
            "appgallery.huawei.com",
        )):
            return hit

        candidate_perms = await fetch_permissions(hit.package_id)
        if not candidate_perms:
            return hit

        sim = permission_jaccard(official_permissions, candidate_perms)
        excess = excess_dangerous(official_permissions, candidate_perms)
        new_reasons = list(hit.reasons)
        bonus = 0

        # Signal 1 — excess dangerous permissions (from s8_sort.py logic)
        if excess:
            pts = min(10, len(excess) * 2)
            bonus += pts
            new_reasons.append(
                f"requests {len(excess)} dangerous permission(s) the official app does not: "
                + ", ".join(excess[:5])
            )

        # Signal 2 — Jaccard similarity. Identical permissions alone are not evidence of a copy, so no points.
        if sim >= 0.85:
            new_reasons.append(
                f"permission set {sim:.0%} matches official app (no points on their own)"
            )
        elif sim >= 0.60:
            bonus += signals.get("permission_similarity_medium", 8)
            new_reasons.append(f"permission set {sim:.0%} overlaps with official app")

        if bonus == 0 and not excess and sim < 0.60:
            return hit

        return hit.model_copy(update={
            "permission_similarity": sim,
            "relevance_score": min(100, hit.relevance_score + bonus),
            "reasons": new_reasons,
        })

    return list(await asyncio.gather(*[_enrich_one(h) for h in hits]))


async def score_hits(
    hits: list[Hit],
    request: ScanRequest,
    official_permissions: list[str] | None = None,
    icon_cache: dict[str, str] | None = None,
    official_publishers: set[str] | frozenset[str] = frozenset(),
    dropped: list[Hit] | None = None,
) -> list[Hit]:
    """Score all hits concurrently, enrich with permission similarity + pHash,
    return sorted by score desc. Hits under the reporting threshold are appended to `dropped` when given,
    so the caller can count them instead of losing them silently."""
    tasks = [score_hit(h, request, official_publishers) for h in hits]
    scored = list(await asyncio.gather(*tasks))

    # Permission similarity enrichment (official permissions read from Google Play)
    if official_permissions:
        scored = await _enrich_permission_similarity(
            scored, official_permissions, request.brand_name, official_package=request.android_package,
        )

    # pHash enrichment — uses pre-populated cache from parallel prefetch if available
    scored = await _enrich_phash(scored, request.brand_name, icon_cache=icon_cache)

    filtered = [h for h in scored if h.relevance_score >= min_relevance_for(h)]
    if dropped is not None:
        dropped.extend(h for h in scored if h.relevance_score < min_relevance_for(h))
    return sorted(filtered, key=lambda h: h.relevance_score, reverse=True)


# Social and web-search hits are noisy: they need a higher score to surface
_NOISY_GROUPS = frozenset({"social", "web_search"})


def min_relevance_for(hit: Hit) -> int:
    """The reporting threshold that applies to this hit's source group."""
    thresholds = _load_config()["thresholds"]
    group = hit.host.group if hit.host else ""
    if group in _NOISY_GROUPS:
        return int(thresholds.get("min_relevance_noisy", 40))
    return int(thresholds["min_relevance"])


def min_relevance() -> int:
    return int(_load_config()["thresholds"]["min_relevance"])


def high_risk_threshold() -> int:
    return int(_load_config()["thresholds"]["high_risk"])


def ruleset_version() -> str:
    return _load_config().get("ruleset_version", "v1")