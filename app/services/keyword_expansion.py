# app/services/keyword_expansion.py
"""
Keyword expansion — generates deduplicated, ranked search terms from the
canonical scan request.

Types: original, variant, package, lure, typo, homoglyph, phonetic,
       package_guess.
"""

from __future__ import annotations

import re
import unicodedata

from app.api.models import Keyword, KeywordType, ScanRequest
from app.config import phrases
from app.config.settings import get_settings

# ── Homoglyph substitution table (bidirectional) ──────────────────────────────
# Maps confusable characters to their ASCII baseline for fold-comparison,
# and generates lookalike variants for query expansion.

_HOMOGLYPH_MAP: dict[str, list[str]] = {
    "a": ["а", "ä", "á", "à", "â", "ā", "@"],  # Cyrillic 'а' included
    "e": ["е", "é", "è", "ê", "ë", "ē", "3"],  # Cyrillic 'е'
    "i": ["і", "ì", "í", "î", "ï", "1", "l", "|"],  # Cyrillic 'і'
    "o": ["о", "ó", "ò", "ô", "ö", "0", "ø"],  # Cyrillic 'о'
    "c": ["с", "ç"],  # Cyrillic 'с'
    "p": ["р"],  # Cyrillic 'р'
    "x": ["х"],  # Cyrillic 'х'
    "rn": ["m"],
    "m": ["rn"],
    "vv": ["w"],
    "w": ["vv"],
    "l": ["1", "i", "|"],
    "s": ["5", "$"],
    "g": ["9", "q"],
    "n": ["и"],  # Cyrillic 'и'
    "b": ["ь"],  # Cyrillic soft sign
}

_KEYBOARD_ADJACENT: dict[str, str] = {
    "q": "wa", "w": "qes", "e": "wrd", "r": "etf", "t": "ryg",
    "y": "tuh", "u": "yij", "i": "uok", "o": "ilp", "p": "o",
    "a": "sqz", "s": "awdz", "d": "sefc", "f": "drg", "g": "fht",
    "h": "gyj", "j": "huk", "k": "jil", "l": "ko", "z": "asx",
    "x": "zsd", "c": "xdf", "v": "cfb", "b": "vgn", "n": "bhm",
    "m": "nj",
}


def fold_homoglyphs(text: str) -> str:
    """Fold known homoglyphs/confusables to ASCII baseline for comparison."""
    result = text.lower()
    for ascii_char, confusables in _HOMOGLYPH_MAP.items():
        for c in confusables:
            if len(c) == 1:
                result = result.replace(c, ascii_char)
    # Normalize unicode
    result = unicodedata.normalize("NFKD", result)
    result = "".join(ch for ch in result if not unicodedata.combining(ch))
    return result


def _spacing_variants(term: str) -> list[str]:
    """Return spaced, hyphenated, and concatenated forms of a multi-word term."""
    tokens = term.lower().split()
    if len(tokens) <= 1:
        return []
    variants = set()
    variants.add(" ".join(tokens))
    variants.add("-".join(tokens))
    variants.add("".join(tokens))
    variants.add("_".join(tokens))
    return [v for v in variants if v != term.lower()]


def _interleave(*groups: list[str]) -> list[str]:
    """Round-robin over groups, dropping duplicates — stable order, mixed kinds."""
    out: dict[str, None] = {}
    for i in range(max((len(g) for g in groups), default=0)):
        for g in groups:
            if i < len(g):
                out.setdefault(g[i], None)
    return list(out)


def _typo_variants(term: str, max_variants: int = 5) -> list[str]:
    """Omission, vowel swap, adjacent swap, duplication, keyboard-adjacent substitution.

    Deterministic: the same brand always yields the same variants, interleaved by
    kind so a small cap still covers every kind.
    """
    t = term.lower()
    n = len(t)

    omit = [t[:i] + t[i + 1:] for i in range(n) if len(t) > 3 and t[i].isalnum()]
    vowels = "aeiou"
    vowel = [
        t[:i] + v + t[i + 1:]
        for i, ch in enumerate(t) if ch in vowels
        for v in vowels if v != ch
    ]
    swap = [t[:i] + t[i + 1] + t[i] + t[i + 2:] for i in range(n - 1)]
    dup = [t[:i] + t[i] + t[i:] for i in range(n)]
    keyboard = [
        t[:i] + sub + t[i + 1:]
        for i, ch in enumerate(t) for sub in _KEYBOARD_ADJACENT.get(ch, "")
    ]

    variants = [v for v in _interleave(omit, vowel, swap, dup, keyboard) if v != t and len(v) >= 3]
    return variants[:max_variants]


def _homoglyph_variants(term: str, max_variants: int = 5) -> list[str]:
    """Generate lookalike variants using the homoglyph map (deterministic order)."""
    t = term.lower()
    out: dict[str, None] = {}
    # Single-char substitutions
    for i, ch in enumerate(t):
        for ascii_ch, confusables in _HOMOGLYPH_MAP.items():
            if ch == ascii_ch:
                for c in confusables[:2]:  # limit to avoid explosion
                    if len(c) == 1:
                        out.setdefault(t[:i] + c + t[i + 1:], None)
    # Multi-char patterns
    for multi, replacements in _HOMOGLYPH_MAP.items():
        if len(multi) > 1 and multi in t:
            for r in replacements[:1]:
                out.setdefault(t.replace(multi, r, 1), None)
    out.pop(t, None)
    return list(out)[:max_variants]


def _phonetic_variants(term: str) -> list[str]:
    """Simple phonetic rules — not a full soundex/metaphone implementation."""
    t = term.lower()
    variants: set[str] = set()
    replacements = [
        ("ph", "f"), ("ck", "k"), ("que", "k"), ("ce", "se"),
        ("ci", "si"), ("tion", "shun"), ("ss", "s"), ("tt", "t"),
        ("ee", "e"), ("oo", "u"),
    ]
    for old, new in replacements:
        if old in t:
            variants.add(t.replace(old, new, 1))
    variants.discard(t)
    return list(variants)[:3]


def _package_guesses(brand: str) -> list[str]:
    b = re.sub(r"[^a-z0-9]", "", brand.lower())
    return [
        f"com.{b}.app",
        f"com.{b}.billing",
        f"com.{b}.pro",
        f"com.{b}.lite",
        f"com.{b}.free",
        f"org.{b}.app",
        f"io.{b}.app",
    ]


def expand(request: ScanRequest) -> list[Keyword]:
    """
    Produce deduplicated, ranked keyword list from the scan request.

    Order of precedence: original > variant > package > lure >
                         typo > homoglyph > phonetic > package_guess.
    Total capped at MAX_KEYWORDS.
    """
    seen: set[str] = set()
    results: list[Keyword] = []

    def add(term: str, ktype: KeywordType, reason: str) -> None:
        norm = term.strip().lower()
        if not norm or norm in seen:
            return
        seen.add(norm)
        results.append(Keyword(term=term.strip(), type=ktype, reason=reason))

    brand = request.brand_name
    keywords = request.keywords

    # Original
    add(brand, KeywordType.original, "Brand name")
    for token in brand.split():
        add(token, KeywordType.original, "Brand name token")
    for kw in keywords:
        add(kw, KeywordType.original, "User-supplied keyword")

    # Variants (spacing)
    for v in _spacing_variants(brand):
        add(v, KeywordType.variant, f"Spacing variant of '{brand}'")

    # Package tokens
    for pkg in [request.android_package, request.ios_bundle]:
        if pkg:
            for token in pkg.split("."):
                if len(token) >= 3 and token not in ("com", "org", "net", "io", "app"):
                    add(token, KeywordType.package, f"Token from package ID '{pkg}'")

    # Lure terms — brand name only, not caller-supplied keywords. English first, then the phrases of the
    # markets of the storefronts every scan searches (config/phrases.yaml).
    for suffix in phrases.lure_suffixes(get_settings().default_country_list()):
        add(f"{brand} {suffix}", KeywordType.lure, f"APK distribution lure ({suffix})")

    # Typos — brand name only
    for v in _typo_variants(brand):
        add(v, KeywordType.typo, f"Typo variant of '{brand}'")

    # Homoglyphs — brand name only
    for v in _homoglyph_variants(brand):
        add(v, KeywordType.homoglyph, f"Homoglyph variant of '{brand}'")

    # Phonetic — brand name only
    for v in _phonetic_variants(brand):
        add(v, KeywordType.phonetic, f"Phonetic variant of '{brand}'")

    # Package guesses
    for v in _package_guesses(brand):
        add(v, KeywordType.package_guess, f"Package ID guess for '{brand}'")

    max_kw = get_settings().max_keywords
    return results[:max_kw]