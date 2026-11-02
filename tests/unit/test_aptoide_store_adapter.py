"""The store-search adapter builds the store's own request (ws2-cache, aab=1, English names)."""

from __future__ import annotations

from app.sources.aptoide_store_adapter import AptoideStoreAdapter

VERIFIED = (
    "https://ws2-cache.aptoide.com/api/7/apps/search?cdn=web"
    "&q=bXlDUFU9YXJtNjQtdjhhLGFybWVhYmktdjdhLGFybWVhYmkmbGVhbmJhY2s9MA&aab=1&language=en_US"
    "&query=PhonePe&limit=20&offset=0&origin=SITE&store_name=aptoide-web"
)


def test_url_matches_the_store_request_that_returned_phone2pc_first():
    assert AptoideStoreAdapter()._url("PhonePe") == VERIFIED


def test_term_is_url_encoded():
    assert "query=Phone+Pe&" in AptoideStoreAdapter()._url("Phone Pe")
