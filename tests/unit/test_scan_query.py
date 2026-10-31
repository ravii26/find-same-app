"""The report's query is the API request as submitted; a confirmed brand's merge does not change it."""

from __future__ import annotations

import datetime

from app.api.models import ScanQuery, ScanRequest


def test_query_passes_through_every_submitted_field():
    body = {"brand_name": "YONO SBI", "brand_id": "brd_x", "new_api_field": [1, 2], "keywords": ["x"]}
    query = ScanQuery(**body, received_at=datetime.datetime.now(datetime.UTC))
    dumped = query.model_dump()
    assert dumped["new_api_field"] == [1, 2]
    assert dumped["brand_id"] == "brd_x"


def test_submitted_body_is_not_part_of_the_dedup_hash():
    plain = ScanRequest(brand_name="YONO SBI")
    with_submitted = plain.model_copy(update={"submitted": {"brand_name": "YONO SBI", "brand_id": "brd_x"}})
    assert plain.dedup_hash() == with_submitted.dedup_hash()
    assert "submitted" not in with_submitted.model_dump()
