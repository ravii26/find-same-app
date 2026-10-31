# app/analysis/apk_similarity.py
"""APK permission similarity — ported from stages/s8_sort.py and stages/s0_discovery.py.

Two signals:
  1. Excess dangerous permissions — permissions the candidate requests that the
     official app does NOT, filtered to those matching known dangerous categories
     (SMS, contacts, camera, microphone, accessibility, etc.).
  2. Permission Jaccard — overall overlap between the two sets.

Official permissions are fetched automatically from Google Play using
google_play_scraper.permissions() — no APK upload required.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor


def jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="perm_sim")

# Dangerous permission categories — substring match on human-readable Play Store labels
_DANGEROUS_KEYWORDS: list[tuple[str, ...]] = [
    ("sms", "text message", "receive messages"),
    ("contacts",),
    ("call log",),
    ("record audio",),
    ("take pictures", "camera"),
    ("accessibility",),
    ("install", "install apps"),
    ("biometric", "fingerprint"),
    ("precise location", "gps"),
]


# Manifest permission constants (as read from a real APK) that are high-risk for
# a lookalike to request beyond the official app. Short names, no android.permission. prefix.
ANDROID_DANGEROUS: frozenset[str] = frozenset({
    "READ_SMS", "RECEIVE_SMS", "SEND_SMS", "READ_CONTACTS", "READ_CALL_LOG",
    "RECORD_AUDIO", "CAMERA", "BIND_ACCESSIBILITY_SERVICE", "REQUEST_INSTALL_PACKAGES",
    "USE_BIOMETRIC", "USE_FINGERPRINT", "ACCESS_FINE_LOCATION", "SYSTEM_ALERT_WINDOW",
    "BIND_DEVICE_ADMIN", "READ_PHONE_STATE", "READ_PHONE_NUMBERS", "PROCESS_OUTGOING_CALLS",
    "MANAGE_EXTERNAL_STORAGE", "BIND_NOTIFICATION_LISTENER_SERVICE",
})


def _short(perm: str) -> str:
    return perm.rsplit(".", 1)[-1].upper()


def android_excess_dangerous(official: list[str], candidate: list[str]) -> list[str]:
    """Dangerous manifest permissions the candidate requests that the official APK does not."""
    official_short = {_short(p) for p in official}
    return sorted({
        p for p in candidate
        if _short(p) not in official_short and _short(p) in ANDROID_DANGEROUS
    })


def android_permission_jaccard(official: list[str], candidate: list[str]) -> float:
    if not official:
        return 0.0
    return round(jaccard({_short(p) for p in official}, {_short(p) for p in candidate}), 3)


def _is_dangerous(perm: str) -> bool:
    p = perm.lower()
    return any(any(kw in p for kw in group) for group in _DANGEROUS_KEYWORDS)


def _normalise(perm_data: dict | list) -> list[str]:
    """Flatten google_play_scraper permissions() dict → sorted list of uppercased descriptions.

    Input (dict):  {"Storage": ["Read the contents of your USB storage", ...], ...}
    Input (list):  ["READ CONTACTS", ...]  — already flat
    Output:        ["READ THE CONTENTS OF YOUR USB STORAGE", ...]
    """
    out: set[str] = set()
    if isinstance(perm_data, dict):
        for group_perms in perm_data.values():
            for p in (group_perms or []):
                if isinstance(p, str):
                    out.add(p.strip().upper())
    elif isinstance(perm_data, list):
        for p in perm_data:
            if isinstance(p, str):
                out.add(p.strip().upper())
    return sorted(out)


# ── Fetch helpers ──────────────────────────────────────────────────────────────

def _fetch_permissions_sync(package_id: str) -> list[str]:
    """Blocking — run in thread pool. Uses google_play_scraper.permissions()."""
    try:
        from google_play_scraper import permissions as gps_perms
        data = gps_perms(package_id, lang="en")
        return _normalise(data)
    except Exception as exc:
        # A package that is not on Google Play (third-party/sideloaded apps) is expected: the library then
        # raises NotFoundError or a TypeError from an empty page. Anything else is a real failure.
        level = log.info if isinstance(exc, TypeError) or type(exc).__name__ == "NotFoundError" else log.warning
        level("permissions lookup failed for %r: %s: %s", package_id, type(exc).__name__, exc)
        return []


def _fetch_developer_sync(package_id: str, country: str = "us") -> str | None:
    """Blocking — run in thread pool. The developer name on the package's own Google Play listing."""
    try:
        from google_play_scraper import app as gps_app
        info = gps_app(package_id, lang="en", country=country)
        name = (info or {}).get("developer")
        return (name.strip() or None) if isinstance(name, str) else None
    except Exception as exc:
        # Not on Play (or Play unreachable): the caller falls back to whatever publishers it was given
        log.info("developer lookup failed for %r: %s: %s", package_id, type(exc).__name__, exc)
        return None


async def fetch_developer(package_id: str) -> str | None:
    """Async wrapper — the official developer name read from the brand's own Play listing."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _fetch_developer_sync, package_id)


async def fetch_permissions(package_id: str) -> list[str]:
    """Async wrapper — fetch Play Store permissions for a package."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _fetch_permissions_sync, package_id)

