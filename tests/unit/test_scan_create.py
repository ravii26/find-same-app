"""POST /v1/scans takes only a brand name; every other detail comes from the brand record."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.api.models import ScanCreate


def test_brand_name_alone_is_accepted():
    assert ScanCreate(brand_name="Bob").brand_name == "Bob"


@pytest.mark.parametrize("name", ["", "   "])
def test_blank_brand_name_is_refused(name):
    with pytest.raises(ValidationError):
        ScanCreate(brand_name=name)


def test_missing_brand_name_is_refused():
    with pytest.raises(ValidationError):
        ScanCreate()


@pytest.mark.parametrize("field, value", [
    ("brand_id", "brd_01M6AR13133EW04WNNMBMY5MTX"),
    ("brand_aliases", ["Bobby"]),
    ("android_packages", ["com.bankofbaroda.mconnect"]),
    ("ios_bundles", ["id1191046038"]),
    ("official_domain", "bankofbaroda.bank.in"),
    ("official_domains", ["bankofbaroda.bank.in"]),
    ("official_publishers", ["Bank of Baroda"]),
    ("allowlist_packages", ["com.example.app"]),
    ("play_developer_ids", ["123"]),
    ("keywords", ["login"]),
    ("countries", ["IN"]),
])
def test_every_other_field_is_refused(field, value):
    with pytest.raises(ValidationError):
        ScanCreate(brand_name="Bob", **{field: value})
