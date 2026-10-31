"""Unit tests for the shared HTTP client."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.utils.http_client import (
    BlockedError,
    FetchError,
    decode_response,
    fetch,
    get_client,
    close_client,
)


def _fake_stream_resp(status_code: int, content: bytes = b"hello", headers: dict | None = None):
    """Build an httpx.Response mock that streams content."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = httpx.Headers(headers or {"content-type": "text/html"})
    resp.request = MagicMock()

    async def aiter_bytes(chunk_size=8192):
        yield content

    resp.aiter_bytes = aiter_bytes
    resp.raise_for_status = MagicMock()
    return resp


@pytest.mark.asyncio
async def test_fetch_download_url_rejected():
    with pytest.raises(FetchError, match="download"):
        await fetch("https://example.com/app.apk")


@pytest.mark.asyncio
async def test_fetch_ssrf_blocked():
    with pytest.raises(FetchError):
        await fetch("http://169.254.169.254/latest/meta-data")


def _make_stream_cm(mock_resp):
    """Return an async context manager that yields mock_resp."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _stream(method, url, headers=None):
        yield mock_resp

    return _stream


@pytest.mark.asyncio
async def test_fetch_success():
    mock_resp = _fake_stream_resp(200, b"<html>content</html>")

    with patch("app.utils.http_client.check_url"), \
         patch("app.utils.http_client.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.stream = _make_stream_cm(mock_resp)
        mock_get_client.return_value = mock_client

        resp = await fetch("https://example.com/page")
        assert resp.status_code == 200
        assert b"content" in resp.content


@pytest.mark.asyncio
async def test_fetch_403_raises_blocked():
    mock_resp = _fake_stream_resp(403)

    with patch("app.utils.http_client.check_url"), \
         patch("app.utils.http_client.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.stream = _make_stream_cm(mock_resp)
        mock_get_client.return_value = mock_client

        with pytest.raises(BlockedError):
            await fetch("https://example.com/page")


@pytest.mark.asyncio
async def test_fetch_429_raises_blocked():
    mock_resp = _fake_stream_resp(429)

    with patch("app.utils.http_client.check_url"), \
         patch("app.utils.http_client.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.stream = _make_stream_cm(mock_resp)
        mock_get_client.return_value = mock_client

        with pytest.raises(BlockedError):
            await fetch("https://example.com/page")


@pytest.mark.asyncio
async def test_fetch_size_cap_exceeded():
    big_content = b"x" * 6_000_000

    async def aiter_bytes(chunk_size=8192):
        yield big_content

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = httpx.Headers({})
    mock_resp.request = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.aiter_bytes = aiter_bytes

    with patch("app.utils.http_client.check_url"), \
         patch("app.utils.http_client.get_client") as mock_get_client:
        mock_client = MagicMock()
        mock_client.stream = _make_stream_cm(mock_resp)
        mock_get_client.return_value = mock_client

        with pytest.raises(FetchError, match="exceeds"):
            await fetch("https://example.com/page", max_bytes=100)


def test_decode_response_charset_header():
    content = "héllo wörld".encode("latin-1")
    resp = httpx.Response(
        200,
        content=content,
        headers={"content-type": "text/html; charset=latin-1"},
        request=httpx.Request("GET", "https://example.com"),
    )
    text = decode_response(resp)
    assert "héllo" in text


def test_decode_response_no_charset_autodetect():
    content = "hello world".encode("utf-8")
    resp = httpx.Response(
        200,
        content=content,
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", "https://example.com"),
    )
    text = decode_response(resp)
    assert "hello" in text


@pytest.mark.asyncio
async def test_get_client_returns_client():
    import app.utils.http_client as hc
    hc._client = None
    client = await get_client()
    assert client is not None
    assert not client.is_closed


@pytest.mark.asyncio
async def test_close_client():
    import app.utils.http_client as hc
    hc._client = None
    await get_client()
    await close_client()
    assert hc._client is None
