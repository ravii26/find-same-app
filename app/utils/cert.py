# app/utils/cert.py
"""Signing-certificate fingerprint helpers.

Stores expose different digests (Aptoide/VirusTotal give SHA-1, our
analyzer can give both), and a caller's official fingerprint may be either.
Digests of different algorithms are never comparable, so mismatched lengths
yield None (unknown) rather than False (mismatch).
"""

from __future__ import annotations


def normalize_fingerprint(value: str | None) -> str | None:
    """Strip separators and uppercase. Returns None for empty input."""
    if not value:
        return None
    cleaned = value.replace(":", "").replace(" ", "").strip().upper()
    return cleaned or None


def compare_cert(
    official: str | None,
    *,
    sha1: str | None = None,
    sha256: str | None = None,
) -> bool | None:
    """True/False when the official fingerprint can be compared, else None."""
    off = normalize_fingerprint(official)
    if not off:
        return None
    if len(off) == 40:
        cand = normalize_fingerprint(sha1)
    elif len(off) == 64:
        cand = normalize_fingerprint(sha256)
    else:
        return None
    if not cand:
        return None
    return cand == off