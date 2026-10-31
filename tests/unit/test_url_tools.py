"""Unit tests for URL normalization, dedup, and link extraction."""

from __future__ import annotations

from app.utils.url_tools import (
    decode_idn,
    extract_links,
    is_download_url,
    normalize_url,
    registrable_domain,
    url_id,
)


def test_normalize_removes_fragment():
    assert normalize_url("https://example.com/path#section") == "https://example.com/path"


def test_normalize_strips_tracking_params():
    url = "https://example.com/?utm_source=google&q=test"
    result = normalize_url(url)
    assert "utm_source" not in result
    assert "q=test" in result


def test_normalize_lowercase_host():
    assert normalize_url("https://EXAMPLE.COM/path") == "https://example.com/path"


def test_normalize_removes_trailing_slash():
    assert normalize_url("https://example.com/") == "https://example.com/"  # root keeps slash
    assert normalize_url("https://example.com/path/") == "https://example.com/path"


def test_url_id_stable():
    u = "https://apkmirror.com/app/petpooja"
    assert url_id(u) == url_id(u)


def test_url_id_same_after_normalize():
    u1 = "https://example.com/path?utm_source=x"
    u2 = "https://example.com/path"
    assert url_id(u1) == url_id(u2)


def test_is_download_url_apk():
    assert is_download_url("https://example.com/file.apk") is True


def test_is_download_url_xapk():
    assert is_download_url("https://example.com/app.xapk") is True


def test_is_download_url_html_false():
    assert is_download_url("https://example.com/page.html") is False


def test_is_download_url_no_extension():
    assert is_download_url("https://example.com/app") is False


def test_registrable_domain_basic():
    assert registrable_domain("https://www.example.com/path") == "example.com"


def test_registrable_domain_co_uk():
    assert registrable_domain("https://example.co.uk") == "example.co.uk"


def test_extract_links_absolute():
    html = '<a href="https://example.com/page">link</a>'
    links = extract_links("https://base.com", html)
    assert "https://example.com/page" in links


def test_extract_links_relative():
    html = '<a href="/path/page">link</a>'
    links = extract_links("https://base.com", html)
    assert "https://base.com/path/page" in links


def test_extract_links_skips_download():
    html = '<a href="/file.apk">download</a><a href="/page.html">page</a>'
    links = extract_links("https://base.com", html)
    assert not any(l.endswith(".apk") for l in links)
    assert "https://base.com/page.html" in links


def test_extract_links_skips_javascript():
    html = '<a href="javascript:void(0)">js</a><a href="/ok">ok</a>'
    links = extract_links("https://base.com", html)
    assert "https://base.com/ok" in links
    assert not any("javascript" in l for l in links)


def test_extract_links_deduplicates():
    html = '<a href="/page">1</a><a href="/page">2</a>'
    links = extract_links("https://base.com", html)
    assert links.count("https://base.com/page") == 1


def test_decode_idn_punycode():
    result = decode_idn("xn--nxasmq6b.com")
    assert result != "xn--nxasmq6b.com"  # should be decoded to unicode


def test_decode_idn_ascii():
    assert decode_idn("example.com") == "example.com"
