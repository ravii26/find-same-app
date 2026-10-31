"""Unit tests for SSRF guard."""

from __future__ import annotations

import pytest

from app.utils.ssrf_guard import SSRFError, check_url, check_ip


# Safe URLs
def test_safe_public_url():
    assert check_url("https://example.com/path") == "https://example.com/path"


def test_safe_http_url():
    assert check_url("http://example.com") == "http://example.com"


# Blocked schemes
def test_ftp_blocked():
    with pytest.raises(SSRFError, match="Scheme"):
        check_url("ftp://example.com")


def test_file_blocked():
    with pytest.raises(SSRFError, match="Scheme"):
        check_url("file:///etc/passwd")


# Blocked IPs via check_ip
def test_localhost_blocked():
    with pytest.raises(SSRFError):
        check_ip("127.0.0.1")


def test_private_10_blocked():
    with pytest.raises(SSRFError):
        check_ip("10.0.0.1")


def test_private_192_blocked():
    with pytest.raises(SSRFError):
        check_ip("192.168.1.1")


def test_private_172_blocked():
    with pytest.raises(SSRFError):
        check_ip("172.16.0.1")


def test_link_local_blocked():
    with pytest.raises(SSRFError):
        check_ip("169.254.169.254")


def test_ipv6_loopback_blocked():
    with pytest.raises(SSRFError):
        check_ip("::1")


def test_ipv6_link_local_blocked():
    with pytest.raises(SSRFError):
        check_ip("fe80::1")


def test_ipv6_unique_local_blocked():
    with pytest.raises(SSRFError):
        check_ip("fc00::1")


# Safe IPs
def test_public_ipv4_allowed():
    check_ip("8.8.8.8")   # Google DNS


def test_public_ipv4_cloudflare():
    check_ip("1.1.1.1")


# URL with no hostname
def test_no_hostname():
    with pytest.raises(SSRFError):
        check_url("https:///path")


# Protocol-relative not allowed (no scheme)
def test_scheme_only_blocked():
    with pytest.raises(SSRFError):
        check_url("javascript:alert(1)")
