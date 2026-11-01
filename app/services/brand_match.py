# app/services/brand_match.py
"""Brand matching shared by scoring and source adapters.

Two levels of "this mentions the brand":

  exact  the brand — or, for multi-word brands, every distinctive word — appears
         (case/accents/spacing-insensitive, digit and Cyrillic lookalikes folded).
         Generic words ("pay", "bank", "app") are not distinctive, so a brand like
         "Google Pay" is not matched by any "... Pay" app, nor "HDFC Bank" by any bank.
  fuzzy  a word in the text is within a small edit distance of the brand
         ("PhonePay" for "PhonePe"). On its own this is weak evidence, so for app
         metadata it must be corroborated by a second field (name + package, name +
         publisher, ...) or be very close.

Free text (a Telegram post, a video title) uses `mentions_any`, which accepts
exact or near matches of any distinctive token.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

from rapidfuzz.distance import Levenshtein

from app.utils.similarity import _norm

# Words too common to identify a brand on their own.
FUZZY_MIN_SIM = 0.74       # normalized Levenshtein for a "spelled like the brand" token
FUZZY_STRONG_SIM = 0.86    # close enough to count without a second corroborating field
FREE_TEXT_MIN_SIM = 0.74   # pre-filter for free text: favour recall, scoring decides
MIN_FUZZY_LEN = 5          # shorter brands: exact only (edit distance is meaningless at 3-4 chars)


def fold(text: str | None) -> str:
    """Lowercase, strip accents, fold lookalikes to one skeleton.

    Digit/Cyrillic lookalikes (via utils.similarity), then the classic ASCII swaps:
    l/I/1/| -> i ("SBl" == "SBI"), rn -> m, vv -> w. Applied to both sides of every
    comparison, so only consistency matters, not the skeleton's readability.
    """
    s = unicodedata.normalize("NFKD", text or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = _norm(s).replace("|", "i").replace("l", "i")
    return s.replace("rn", "m").replace("vv", "w")


_GENERIC_RAW = ({
    "app", "apps", "pay", "payment", "payments", "bank", "banking", "mobile", "money", "wallet",
    "online", "india", "official", "pro", "lite", "the", "inc", "ltd", "limited", "pvt", "private",
    "co", "corp", "group", "digital", "finance", "financial", "services", "service", "tech",
    "technologies", "games", "game", "global", "plus", "free", "new", "and", "for", "of",
})
GENERIC_WORDS = frozenset(fold(w) for w in _GENERIC_RAW)


def compact(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", fold(text))


def _words(text: str | None) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", fold(text)) if w]


@lru_cache(maxsize=512)
def distinctive_tokens(brand: str) -> tuple[str, ...]:
    toks = [w for w in _words(brand) if len(w) >= 3]
    distinctive = [w for w in toks if w not in GENERIC_WORDS]
    return tuple(distinctive or toks)


def _best_sim(target: str, text: str | None) -> float:
    """Best similarity of `target` to any word, or adjacent word pair, of `text`."""
    # Generic words ("finance", "bank") are ignored: they are common English, and
    # near a brand like "Binance" by accident, not by spoofing.
    all_words = [w for w in _words(text) if w not in GENERIC_WORDS]
    cands = [w for w in all_words if len(w) >= 4]
    cands += [a + b for a, b in zip(all_words, all_words[1:])]  # "phone pe" -> "phonepe"
    return max((Levenshtein.normalized_similarity(target, c) for c in cands), default=0.0)


# Brands this short or shorter must match a whole word (optionally plus a lure suffix/prefix):
# "uber" is a real match in "UberPro" or "Uber Driver", not in "Uberaba" or "Tuberculosis".
SHORT_BRAND_LEN = 5
_LURE_SUFFIXES_RAW = ({
    "app", "apps", "apk", "pro", "lite", "plus", "mod", "free", "cash", "pay", "clone", "driver",
    "partner", "business", "official", "hack", "premium", "money", "wallet", "online", "india",
    "web", "wa", "new", "vip", "gold", "max", "prime", "rewards", "bonus", "offer", "login",
})
_LURE_PREFIXES_RAW = ({"get", "my", "go", "the", "new", "true", "i", "e", "mi", "gb", "fm"})

_LURE_SUFFIXES = frozenset(fold(w) for w in _LURE_SUFFIXES_RAW)
_LURE_PREFIXES = frozenset(fold(w) for w in _LURE_PREFIXES_RAW)


def _short_word_match(token: str, text: str) -> bool:
    for w in _words(text):
        if w == token:
            return True
        if w.startswith(token) and w[len(token):] in _LURE_SUFFIXES:
            return True
        if w.endswith(token) and w[: -len(token)] in _LURE_PREFIXES:
            return True
    # brand split across words in the text ("uber" never is, but "g pay" style) handled by compact join
    return False


def _token_in(token: str, text: str) -> bool:
    if len(token) <= SHORT_BRAND_LEN:
        return _short_word_match(token, text)
    return token in compact(text)


def _exact(brand: str, text: str) -> bool:
    c = compact(text)
    if not c:
        return False
    bc = compact(brand)
    if len(bc) > SHORT_BRAND_LEN:
        if bc in c:
            return True
    elif _short_word_match(bc, text):
        return True
    toks = distinctive_tokens(brand)
    return bool(toks) and all(_token_in(t, text) for t in toks)


def presence(brand: str, name: str | None, package: str | None, publisher: str | None) -> str | None:
    """'exact', 'fuzzy', 'fuzzy_weak' or None for how strongly app metadata matches the brand."""
    fields = [f for f in (name, package, publisher) if f]
    if not fields or not brand:
        return None
    joined = " ".join(fields)
    if any(_exact(brand, f) for f in fields) or _exact(brand, joined):
        return "exact"

    target = "".join(distinctive_tokens(brand))
    if len(target) < MIN_FUZZY_LEN:
        return None
    sims = [_best_sim(target, f) for f in fields]
    close = sum(1 for s in sims if s >= FUZZY_MIN_SIM)
    if max(sims, default=0) >= FUZZY_STRONG_SIM or close >= 3:
        return "fuzzy"        # very close, or the misspelling repeats in name, package and publisher
    if close >= 2:
        return "fuzzy_weak"   # two fields only: common words (Phonemes, Lip Art) land here too
    return None


_STRENGTH = {"exact": 3, "fuzzy": 2, "fuzzy_weak": 1, None: 0}


def presence_any(brands: list[str], name: str | None, package: str | None, publisher: str | None) -> str | None:
    """The strongest presence over the brand name and its aliases ("YONO" for "YONO SBI")."""
    results = [presence(b, name, package, publisher) for b in brands if b]
    return max(results, key=_STRENGTH.get, default=None)


def mentions_any(text: str | None, tokens) -> bool:
    """Does free text mention any brand token, exactly or as a near misspelling?"""
    if not text:
        return False
    folded_tokens = [fold(t) for t in tokens if t]
    if any(compact(t) and _token_in(compact(t), text) for t in folded_tokens):
        return True
    for t in folded_tokens:
        t = compact(t)
        if len(t) >= MIN_FUZZY_LEN and _best_sim(t, text) >= FREE_TEXT_MIN_SIM:
            return True
    return False


def brand_terms(ctx, kinds=("original", "package"), limit: int = 4, variants: int = 3) -> list[str]:
    """Search terms for store APIs: brand-bearing keywords first, then a few spelling variants.

    Variants (typo, homoglyph, phonetic, spacing) are derived from the brand, so they are
    safe to search; store search is fuzzy enough that they surface lookalike spellings.
    """
    toks = distinctive_tokens(ctx.request.brand_name)
    primary: list[str] = []
    for kw in ctx.expanded_keywords:
        if kw.type.value in kinds and any(t in compact(kw.term) for t in toks):
            primary.append(kw.term)
    extra = [
        kw.term for kw in ctx.expanded_keywords
        if kw.type.value in ("typo", "homoglyph", "phonetic", "variant")
    ][:variants]
    out: list[str] = []
    for t in primary[:limit] + extra:
        if t.lower() not in {o.lower() for o in out}:
            out.append(t)
    return out


def exact_in(brand: str, text: str | None) -> bool:
    """Does `text` contain the brand exactly (no fuzzy matching)?"""
    return bool(text) and _exact(brand, text)


def count_mentions(text: str | None, brand: str) -> int:
    """How many times the brand appears in free text (exact; short brands as whole words)."""
    if not text:
        return 0
    bc = compact(brand)
    if len(bc) > SHORT_BRAND_LEN:
        return compact(text).count(bc)
    return sum(1 for w in _words(text) if w == bc or _short_word_match(bc, w))
