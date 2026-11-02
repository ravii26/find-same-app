"""Brand aliases: an app named after an alias counts as the brand, and the threshold label matches the source group."""

from __future__ import annotations

import datetime

from app.api.models import Hit, HostInfo, ScanRequest
from app.storage import brand_index
from app.services.brand_match import presence_any
from app.services.scoring import _score_one, min_relevance_for

NO_ALIAS_REASON = "brand name absent from title/package/publisher"


def _score(name: str, publisher: str | None = "Yono Games", aliases: tuple[str, ...] = ()) -> tuple[int, list[str]]:
    return _score_one(
        name, name, None, publisher,
        "https://www.apkpure.com/x", "apk_site",
        "YONO SBI", None, None,
        False, False, None,
        aliases,
    )


def test_alias_is_needed_for_an_app_named_only_after_it():
    assert presence_any(["YONO SBI"], "Yono Rummy Store", None, None) is None
    assert presence_any(["YONO SBI", "YONO"], "Yono Rummy Store", None, None) == "exact"


def test_brand_absent_cap_applies_without_alias():
    _, reasons = _score("Yono Rummy Store")
    assert any(NO_ALIAS_REASON in r for r in reasons)


def test_alias_lifts_the_brand_absent_cap():
    score, reasons = _score("Yono Rummy Store", aliases=("YONO",))
    assert not any(NO_ALIAS_REASON in r for r in reasons)
    assert score > 34


def test_near_miss_name_is_not_an_exact_alias_match():
    # "Yonomi" is a near miss of "YONO", so it is at most a fuzzy match, never exact
    assert presence_any(["YONO SBI", "YONO"], "Yonomi Smart Home Automation", None, "Yonomi Inc") != "exact"


def test_threshold_follows_the_source_group():
    social = Hit(id="1", type="web_page", url="https://x.example", relevance_score=38,
                 host=HostInfo(group="search_engine", name="Bing", domain="bing.com"),
                 discovered_at=datetime.datetime.now(datetime.UTC))
    store = social.model_copy(update={"host": HostInfo(group="app_store", name="Play", domain="play.google.com")})
    assert min_relevance_for(social) == 40
    assert min_relevance_for(store) == 35


def test_aliases_reach_the_scan_request_through_the_brand_record():
    doc = {"brand_name": "YONO SBI", "aliases": ["YONO"]}
    merged = brand_index.merge_into_request(ScanRequest(brand_name="YONO SBI", brand_aliases=["Yono Bank"]), doc, [])
    assert merged.brand_aliases == ["Yono Bank", "YONO"]


def test_aliases_change_the_dedup_hash():
    base = ScanRequest(brand_name="YONO SBI")
    with_alias = ScanRequest(brand_name="YONO SBI", brand_aliases=["YONO"])
    assert base.dedup_hash() != with_alias.dedup_hash()
