# app/utils/similarity.py
"""Text and domain similarity using Jaro-Winkler (jellyfish) and tldextract."""

from __future__ import annotations

import tldextract as _tldx

_tld = _tldx.TLDExtract(suffix_list_urls=())  # bundled snapshot — no network call

_HOMOGLYPH_MAP = str.maketrans({
    "1": "l", "0": "o", "3": "e", "4": "a", "5": "s",
    "7": "t", "8": "b", "9": "g",
    # Cyrillic → Latin lookalikes
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "і": "i", "ј": "j", "һ": "h",
    "ѕ": "s", "ї": "i",
})


def _norm(text: str) -> str:
    return text.lower().translate(_HOMOGLYPH_MAP)


def domain_lookalike(candidate_url: str, official_domains: list[str]) -> float:
    """Return 0.0–1.0 JW similarity between candidate's registered domain label
    and any official domain label.  Returns 0.0 when the domain IS official
    (so callers treat 0.0 = not a lookalike)."""
    import jellyfish

    if not candidate_url or not official_domains:
        return 0.0

    host = candidate_url.split("//")[-1].split("/")[0].split(":")[0]
    ext = _tld(host)
    label = ext.domain.lower()
    if not label:
        return 0.0

    official_labels = {_tld(d).domain.lower() for d in official_domains if d}
    if label in official_labels:
        return 0.0  # exact match → it IS official, not a lookalike

    label_norm = _norm(label)
    best = 0.0
    for ol in official_labels:
        if not ol:
            continue
        ol_norm = _norm(ol)
        # Substring containment is a very strong lookalike signal
        if ol_norm in label_norm and label_norm != ol_norm:
            best = max(best, 0.92)
            continue
        jw = jellyfish.jaro_winkler_similarity(label_norm, ol_norm)
        best = max(best, jw)

    return round(best, 2) if best >= 0.75 else 0.0
