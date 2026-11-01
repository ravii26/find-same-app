"""Official domains read from a Google Play listing: used for the scan, never from shared hosts, never stored."""

from __future__ import annotations

from app.services.play_listing import listing_domains, listing_icon

BOB_WORLD = {
    "developer": "Bank of Baroda",
    "developerWebsite": "http://www.bankofbaroda.com",
    "developerEmail": "mconnect@bankofbaroda.com",
    "privacyPolicy": "https://www.bobworld.com/privacy-policy",
    "icon": "https://play-lh.googleusercontent.com/icon",
}


def test_website_and_privacy_policy_are_the_brands_domains():
    assert listing_domains(BOB_WORLD) == [("bankofbaroda.com", "developerWebsite"), ("bobworld.com", "privacyPolicy")]


def test_the_email_domain_is_listed_only_when_it_is_new():
    assert ("bankofbaroda.com", "developerEmail") not in listing_domains(BOB_WORLD)
    only_email = {"developerEmail": "support@acme-pay.io"}
    assert listing_domains(only_email) == [("acme-pay.io", "developerEmail")]


def test_shared_hosts_are_never_taken_as_the_brand():
    listing = {"developerWebsite": "https://sites.google.com/view/acme", "developerEmail": "dev@gmail.com",
               "privacyPolicy": "https://acme.github.io/privacy"}
    assert listing_domains(listing) == []


def test_a_bare_host_without_a_scheme_is_read():
    assert listing_domains({"developerWebsite": "www.sbi.co.in"}) == [("sbi.co.in", "developerWebsite")]


def test_missing_or_empty_fields_give_nothing():
    assert listing_domains({}) == []
    assert listing_domains({"developerWebsite": "", "privacyPolicy": None}) == []


def test_subdomains_collapse_to_the_registrable_domain():
    assert listing_domains({"privacyPolicy": "https://legal.shop.example.co.uk/p"}) == [
        ("example.co.uk", "privacyPolicy")]


def test_icon_must_be_a_web_url():
    assert listing_icon(BOB_WORLD) == "https://play-lh.googleusercontent.com/icon"
    assert listing_icon({"icon": "not-a-url"}) is None
    assert listing_icon({}) is None
