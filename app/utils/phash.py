# app/utils/phash.py
"""Perceptual hash utilities for icon similarity comparison."""

from __future__ import annotations

import asyncio
import io
import logging

from app.utils.http_client import FetchError, fetch

log = logging.getLogger(__name__)

# Global cap across all concurrent scans (4 scans × ~4 slots each).
# Prevents icon CDNs from being hammered and keeps memory usage predictable.
_ICON_SEM = asyncio.Semaphore(16)


async def compute_phash(image_url: str, hash_size: int = 8, cache: dict[str, str] | None = None) -> str:
    """Download an image URL and return its 64-bit perceptual hash string."""
    if cache is not None and image_url in cache:
        return cache[image_url]
    try:
        import imagehash
        from PIL import Image

        async with _ICON_SEM:
            resp = await fetch(image_url, rotate_ua=True, retries=1)
        img = Image.open(io.BytesIO(resp.content))
        result = str(imagehash.phash(img, hash_size=hash_size))
    except (FetchError, Exception) as exc:
        log.warning("icon hash failed for %s: %s: %s", image_url, type(exc).__name__, exc)
        result = ""
    if cache is not None:
        cache[image_url] = result
    return result


def phash_similarity(hash_a: str, hash_b: str) -> float:
    """Hamming-distance-based similarity between two pHash hex strings. Returns 0.0–1.0."""
    if not hash_a or not hash_b:
        return 0.0
    try:
        import imagehash

        h1 = imagehash.hex_to_hash(hash_a)
        h2 = imagehash.hex_to_hash(hash_b)
        hamming = h1 - h2
        total_bits = len(hash_a) * 4
        return 1.0 - (hamming / total_bits) if total_bits > 0 else 0.0
    except Exception as exc:
        log.warning("icon hash comparison failed: %s: %s", type(exc).__name__, exc)
        return 0.0
