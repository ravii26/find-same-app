"""An allowlisted hit keeps the developer id it was matched on, so the audit entry shows which account it was."""

from __future__ import annotations

import datetime

from app.api.models import Hit, HitType, HostInfo, ScanRequest
from app.services.allowlist import split_allowlisted, store_metadata

PHONEPE = ScanRequest(brand_name="PhonePe", android_packages=["com.phonepe.app"], official_domain="phonepe.com",
                      official_publishers=["PhonePe"], play_developer_ids=["PhonePe"])


def _play(developer_id: str | None) -> Hit:
    return Hit(
        id="play", type=HitType.app_listing, title="PhonePe Business", app_name="PhonePe Business",
        package_id="com.phonepe.app.business", publisher="PhonePe", developer_id=developer_id,
        url="https://play.google.com/store/apps/details?id=com.phonepe.app.business", relevance_score=60,
        host=HostInfo(group="app_store", name="Google Play", domain="play.google.com", is_official_store=True),
        discovered_at=datetime.datetime.now(datetime.UTC),
    )


def test_allowlisted_item_keeps_the_developer_id():
    meta = store_metadata([("com.phonepe.app.business", "PhonePe", "PhonePe")])
    remaining, allowed = split_allowlisted([_play("PhonePe")], PHONEPE, store_meta=meta)
    assert remaining == [] and allowed[0].developer_id == "PhonePe"


def test_allowlisted_item_without_a_developer_id_is_none():
    meta = store_metadata([("com.phonepe.app.business", "PhonePe", None)])
    _, allowed = split_allowlisted([_play(None)], PHONEPE, store_meta=meta)
    assert allowed and allowed[0].developer_id is None
