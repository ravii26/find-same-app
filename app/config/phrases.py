# app/config/phrases.py
"""Per-market search and scoring phrases, loaded from config/phrases.yaml (see that file for the format)."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "phrases.yaml"
_LISTS = ("lure_suffixes", "lure_words", "scam_words", "search_queries")

# Localized extras added per country, so a request with many countries cannot flood the search.
MAX_LOCALIZED_SUFFIXES = 12
MAX_LOCALIZED_QUERIES = 2


@lru_cache(maxsize=1)
def _load() -> dict[str, Any]:
    data = yaml.safe_load(_PATH.read_text(encoding="utf-8"))
    packs = {"default": data["default"], **data.get("packs", {})}
    for name, pack in packs.items():
        for key in _LISTS:
            values = pack.get(key, [])
            if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
                raise ValueError(f"{_PATH.name}: '{name}.{key}' must be a list of non-empty strings")
    for query_key in ("search_queries",):
        for name, pack in packs.items():
            for q in pack.get(query_key, []):
                if "{brand}" not in q:
                    raise ValueError(f"{_PATH.name}: '{name}.{query_key}' entry {q!r} lacks the {{brand}} placeholder")
    markets = {cc.upper(): list(p) for cc, p in (data.get("markets") or {}).items()}
    for cc, names in markets.items():
        unknown = [n for n in names if n not in packs]
        if unknown:
            raise ValueError(f"{_PATH.name}: market {cc} uses unknown pack(s) {unknown}")
    return {"packs": packs, "markets": markets}


def _packs_for(countries: list[str]) -> list[dict[str, Any]]:
    cfg = _load()
    names: list[str] = []
    for cc in countries:
        for n in cfg["markets"].get(cc.upper(), []):
            if n not in names:
                names.append(n)
    return [cfg["packs"][n] for n in names]


def _localized(key: str, countries: list[str], limit: int) -> list[str]:
    out: list[str] = []
    for pack in _packs_for(countries):
        for value in pack.get(key, [])[:limit]:
            if value not in out:
                out.append(value)
    return out


def lure_suffixes(countries: list[str]) -> list[str]:
    """English suffixes first, then the localized ones of the given countries (the storefronts every scan searches)."""
    base = list(_load()["packs"]["default"]["lure_suffixes"])
    return base + [s for s in _localized("lure_suffixes", countries, MAX_LOCALIZED_SUFFIXES) if s not in base][:MAX_LOCALIZED_SUFFIXES]


def search_queries(countries_one: str) -> list[str]:
    """Localized Bing query templates for ONE country (the English ones are built by the adapter)."""
    return _localized("search_queries", [countries_one], MAX_LOCALIZED_QUERIES)


def _words(key: str) -> list[str]:
    seen: dict[str, None] = {}
    for pack in _load()["packs"].values():
        for w in pack.get(key, []):
            seen.setdefault(w.strip().lower(), None)
    return sorted(seen, key=len, reverse=True)  # longest first so "customer care" wins over "care"


def word_regex(key: str) -> re.Pattern[str]:
    """Whole-word, case-insensitive regex over a word list of ALL packs (scoring is language-agnostic)."""
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in _words(key)) + r")(?!\w)", re.IGNORECASE)