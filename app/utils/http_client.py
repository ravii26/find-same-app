# app/utils/http_client.py
"""Shared async HTTP client with SSRF guard, rate limiting, and UA rotation."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import threading
import time
from collections.abc import Callable
from typing import TypeVar
from urllib.parse import urlparse

import httpcore
import httpx

from app.config.settings import get_settings
from app.storage.redis_client import rate_limit_client
from app.utils.rate_limiter import RateLimiter
from app.utils.ssrf_guard import SSRFError, check_url, resolve_safe
from app.utils.url_tools import is_download_url

log = logging.getLogger(__name__)

T = TypeVar("T")

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]


class FetchError(Exception):
    """Raised when a fetch fails after all retries."""


class BlockedError(FetchError):
    """Raised when a source returns 403/429/CAPTCHA."""


# Adapters flatten exceptions into status text, so a block is recognised downstream by the message the
# raise sites above produce (they can be prefixed by the adapter).
_BLOCKED_TEXT = re.compile(r"Bot protection \(HTTP \d+\)|Rate limited \(429\)|HTTP (401|403) from ")


def is_blocked_message(text: str | None) -> bool:
    """True when an error text says the source refused us (block, rate limit), not that it is down."""
    return bool(text and _BLOCKED_TEXT.search(text))


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """Network backend that connects only to addresses it has just validated.

    The hostname is resolved exactly once, every answer is checked against the blocked ranges, and
    the TCP connection goes to that validated IP. TLS still verifies the certificate against the
    original hostname (httpcore passes it as server_hostname), so there is no second DNS lookup for
    a rebinding answer to slip through, on the first request or on any redirect hop.
    """

    def __init__(self, inner: httpcore.AsyncNetworkBackend) -> None:
        self._inner = inner

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        last: Exception | None = None
        for ip in await resolve_safe(host, port):
            try:
                return await self._inner.connect_tcp(
                    ip, port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except httpcore.ConnectError as exc:
                last = exc
        raise last or httpcore.ConnectError(f"could not connect to {host}")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise SSRFError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _SSRFCheckingTransport(httpx.AsyncHTTPTransport):
    """Transport whose every connection (including each redirect hop) is made to a validated IP."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("trust_env", False)  # no proxy from the environment: we would connect to it instead
        super().__init__(*args, **kwargs)
        pool = self._pool
        pool._network_backend = _PinnedBackend(pool._network_backend)  # httpcore has no public hook for this


def _build_client(settings: object | None = None) -> httpx.AsyncClient:
    s = settings or get_settings()
    timeout = httpx.Timeout(
        connect=s.http_connect_timeout,
        read=s.http_read_timeout,
        write=s.http_read_timeout,
        pool=5.0,
    )
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=10)
    return httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
        follow_redirects=True,
        max_redirects=s.http_max_redirects,
        transport=_SSRFCheckingTransport(),
        headers={"Accept-Encoding": "gzip"},
    )


# Thread-local storage — each Dramatiq worker thread gets its own client and
# rate limiter so parallel scans never share or overwrite each other's state.
_tl = threading.local()


async def get_client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    if getattr(_tl, "client_loop", None) is not loop:
        _tl.client_lock = asyncio.Lock()
        _tl.client_loop = loop
        _tl.client = None
    async with _tl.client_lock:
        if getattr(_tl, "client", None) is None or _tl.client.is_closed:
            _tl.client = _build_client()
    return _tl.client


async def close_client() -> None:
    client = getattr(_tl, "client", None)
    if client and not client.is_closed:
        await client.aclose()
        _tl.client = None
    _tl.client = None
    _tl.client_loop = None


async def close_rate_limiter() -> None:
    """Close the thread-local rate-limiter Redis connection before the event loop closes.

    Must be called at the end of every asyncio.run() block that used fetch().
    Skipping it leaves an unclosed aioredis connection pool whose __del__ tries to schedule
    cleanup callbacks on the now-closed loop, causing 'Event loop is closed' in the next scan
    on the same Dramatiq thread.
    """
    rl = getattr(_tl, "rate_limiter", None)
    if rl is not None:
        try:
            await rl._redis.aclose(close_connection_pool=True)
        except Exception:
            pass
    _tl.rate_limiter = None
    _tl.rl_loop = None


async def _get_rate_limiter() -> RateLimiter | None:
    loop = asyncio.get_running_loop()
    if getattr(_tl, "rl_loop", None) is not loop:
        _tl.rl_lock = asyncio.Lock()
        _tl.rl_loop = loop
        _tl.rate_limiter = None
    if getattr(_tl, "rate_limiter", None) is None:
        async with _tl.rl_lock:
            if getattr(_tl, "rate_limiter", None) is None:
                try:
                    _tl.rate_limiter = RateLimiter(rate_limit_client())
                except Exception as e:
                    log.warning("rate limiter unavailable (fail-open, no throttling): %s", e)
                    return None
    return _tl.rate_limiter



def _is_bot_challenge(headers: httpx.Headers) -> bool:
    """True when the reply carries a CDN bot-protection marker (Cloudflare, Sucuri, DDoS-Guard ...)."""
    server = headers.get("server", "").lower()
    return bool(
        headers.get("cf-mitigated")
        or headers.get("cf-ray")
        or server.startswith(("cloudflare", "sucuri", "ddos-guard"))
    )


async def throttle(url: str) -> None:
    """Wait for this host's rate-limit slot before any request to it.

    Shared by fetch() and the browser (Playwright) paths, so every outbound request to a source goes
    through the same per-domain limit. Fail-open: a Redis problem logs a warning and never blocks a scan.
    """
    host = urlparse(url).hostname or ""
    if not host:
        return
    limiter = await _get_rate_limiter()
    if limiter is None:
        return
    try:
        await limiter.acquire(host)
    except Exception as e:
        log.warning("rate limiter acquire failed for %s (fail-open): %s", host, e)


class _ThrottledError(Exception):
    """A 403 or 429 reply: the source may be blocking us for now. fetch() waits and asks again."""

    def __init__(self, status: int, url: str, retry_after: float | None) -> None:
        super().__init__(f"Rate limited (429) from {url}" if status == 429 else f"HTTP {status} from {url}")
        self.retry_after = retry_after


_MAX_RETRY_AFTER = 30.0  # never sleep longer than this on a Retry-After header

_slots: threading.Semaphore | None = None
_slots_lock = threading.Lock()


def _request_slots() -> threading.Semaphore:
    """Outbound request slots shared by every scan thread in this process (threads cannot share an asyncio lock)."""
    global _slots
    with _slots_lock:
        if _slots is None:
            _slots = threading.Semaphore(get_settings().http_max_concurrency)
        return _slots


async def _acquire_slot() -> None:
    # Polling instead of a blocking acquire: a waiting task can still be cancelled, and never strands a slot
    slots = _request_slots()
    while not slots.acquire(blocking=False):
        await asyncio.sleep(0.05)


# A host we cannot connect to at all (DNS answer refused, connection refused, no route, firewall timeout) will not
# come back within seconds: such errors get one retry, not http_retries. After _UNREACHABLE_AFTER whole fetches to
# the same host failed that way, further fetches to it fail at once for _UNREACHABLE_TTL seconds. Without this, a
# source that is unreachable from this network spends ~30 s on every one of its queries and can use up the whole scan.
_UNREACHABLE_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError)
_UNREACHABLE_RETRIES = 1
_UNREACHABLE_AFTER = 2
_UNREACHABLE_TTL = 300.0


def _unreachable_hosts() -> dict[str, tuple[int, float, str]]:
    """host -> (failed fetches, monotonic time of the last one, last error), per thread and event loop."""
    loop = asyncio.get_running_loop()
    if getattr(_tl, "unreachable_loop", None) is not loop:
        _tl.unreachable_loop = loop
        _tl.unreachable = {}
    return _tl.unreachable


def _skip_reason(host: str) -> str | None:
    """Why fetches to this host are skipped right now, or None."""
    entry = _unreachable_hosts().get(host)
    if not entry:
        return None
    count, last, error = entry
    if count >= _UNREACHABLE_AFTER and time.monotonic() - last < _UNREACHABLE_TTL:
        return f"{host} is unreachable ({count} fetches could not connect; last error: {error}); not tried again"
    return None


def _mark_unreachable(host: str, exc: Exception) -> None:
    hosts = _unreachable_hosts()
    count = hosts.get(host, (0, 0.0, ""))[0]
    hosts[host] = (count + 1, time.monotonic(), f"{type(exc).__name__}: {exc}"[:200])


_BLOCKED_STATUS = re.compile(r"Status code (403|429|5\d\d) returned")


def call_with_backoff(call: Callable[[], T]) -> T:
    """Run a blocking call that does not go through httpx (google_play_scraper) with fetch()'s rules.

    Call it from a worker thread: it waits for a request slot and sleeps between attempts. A 403, 429 or 5xx
    raised by the library is retried with the same backoff as fetch(); any other error is raised at once.
    """
    settings = get_settings()
    for attempt in range(settings.http_retries + 1):
        slots = _request_slots()
        slots.acquire()
        try:
            return call()
        except Exception as exc:
            if attempt >= settings.http_retries or not _BLOCKED_STATUS.search(str(exc)):
                raise
            delay = min(settings.http_backoff_base * (2 ** attempt), _MAX_RETRY_AFTER) + random.uniform(0, 0.5)
            log.info("source refused the request (%s); retrying in %.1fs", exc, delay)
        finally:
            slots.release()
        time.sleep(delay)


async def _get_once(
    client: httpx.AsyncClient,
    url: str,
    method: str,
    data: dict[str, str] | None,
    headers: dict[str, str],
    follow_redirects: bool,
    timeout: float | None,
    cap: int,
) -> httpx.Response:
    """One request. Returns the reply, or raises the error that says why there is no usable reply."""
    stream_kwargs: dict = {"headers": headers, "follow_redirects": follow_redirects}
    if timeout is not None:
        stream_kwargs["timeout"] = timeout
    if method.upper() == "POST" and data:
        stream_kwargs["data"] = data
    async with client.stream(method.upper(), url, **stream_kwargs) as resp:
        if resp.status_code == 401:
            raise BlockedError(f"HTTP 401 from {url}")
        if resp.status_code in (403, 429):
            raise _ThrottledError(resp.status_code, url, _retry_after_seconds(resp.headers.get("retry-after")))
        if resp.status_code >= 500 and _is_bot_challenge(resp.headers):
            # Cloudflare answers 5xx to a client it will not serve: that is "blocked", not "down"
            raise BlockedError(f"Bot protection (HTTP {resp.status_code}) from {url}")
        if 400 <= resp.status_code < 500:
            # The server understood and said no; asking again gets the same answer
            raise FetchError(f"HTTP {resp.status_code} from {url}")
        if not (resp.is_redirect and not follow_redirects):
            resp.raise_for_status()

        # Enforce size cap
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes(chunk_size=8192):
            total += len(chunk)
            if total > cap:
                raise FetchError(f"Response from {url} exceeds {cap} bytes limit")
            chunks.append(chunk)

        # Build a synthetic full response object.
        # aiter_bytes() already decompresses; strip Content-Encoding so
        # httpx.Response doesn't attempt a second decompression pass.
        content = b"".join(chunks)
        reply_headers = dict(resp.headers)
        reply_headers.pop("content-encoding", None)
        return httpx.Response(
            status_code=resp.status_code,
            headers=reply_headers,
            content=content,
            request=resp.request,
        )


async def fetch(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    max_bytes: int | None = None,
    rotate_ua: bool = True,
    retries: int | None = None,
    backoff_base: float | None = None,
    timeout: float | None = None,
    follow_redirects: bool = True,
) -> httpx.Response:
    """
    Fetch a URL with SSRF guard, size cap, UA rotation, concurrency limit and exponential backoff.

    retries and backoff_base default to the http_retries and http_backoff_base settings. A 403, a 429, a 5xx
    and network errors are retried; a 403 or 429 that persists raises BlockedError. Never fetches direct
    download URLs. With follow_redirects=False a 3xx reply is returned as is (callers use it as a signal, e.g.
    t.me answers 302 for channels without a public preview). Raises FetchError on other failures.
    """
    if is_download_url(url):
        raise FetchError(f"Refusing to fetch direct download URL: {url}")

    try:
        # Early, readable failure. Not the security boundary — the pinned backend enforces on connect.
        await asyncio.to_thread(check_url, url)
    except SSRFError as exc:
        raise FetchError(str(exc)) from exc

    merged_headers: dict[str, str] = {}
    if rotate_ua:
        merged_headers["User-Agent"] = random.choice(_USER_AGENTS)
    if headers:
        merged_headers.update(headers)

    settings = get_settings()
    retries = settings.http_retries if retries is None else retries
    backoff_base = settings.http_backoff_base if backoff_base is None else backoff_base
    cap = max_bytes or settings.max_response_bytes
    client = await get_client()

    host = (urlparse(url).hostname or "").lower()
    skip = _skip_reason(host)
    if skip:
        raise FetchError(skip)

    # Per-domain rate limit, shared across all concurrent scans via Redis (see throttle()).
    await throttle(url)

    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(retries + 1):
        if isinstance(last_exc, _UNREACHABLE_ERRORS) and attempt > _UNREACHABLE_RETRIES:
            break  # could not connect at all: more attempts only burn the scan's time
        try:
            # A slot is held only for the request itself, never while sleeping between attempts
            await _acquire_slot()
            try:
                resp = await _get_once(client, url, method, data, merged_headers, follow_redirects, timeout, cap)
            finally:
                _request_slots().release()
            _unreachable_hosts().pop(host, None)
            return resp
        except (BlockedError, FetchError):
            raise
        except SSRFError as exc:  # a redirect hop (or rebinding answer) pointed somewhere forbidden
            raise FetchError(f"blocked by SSRF guard: {exc}") from exc
        except _ThrottledError as exc:
            # The source is refusing us for now: wait, then ask again. Retry-After wins over our own schedule.
            last_exc = exc
            if attempt < retries:
                delay = exc.retry_after if exc.retry_after is not None else backoff_base * (2 ** attempt)
                await asyncio.sleep(min(delay, _MAX_RETRY_AFTER) + random.uniform(0, 0.5))
        except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            gives_up = isinstance(exc, _UNREACHABLE_ERRORS) and attempt >= _UNREACHABLE_RETRIES
            if attempt < retries and not gives_up:
                delay = backoff_base * (2 ** attempt) + random.uniform(0, 0.5)
                await asyncio.sleep(delay)

    if isinstance(last_exc, _ThrottledError):
        raise BlockedError(str(last_exc)) from last_exc
    if isinstance(last_exc, _UNREACHABLE_ERRORS):
        _mark_unreachable(host, last_exc)
        raise FetchError(f"Could not connect to {host} for {url}: {type(last_exc).__name__}: {last_exc}") from last_exc
    raise FetchError(f"All {retries + 1} attempts failed for {url}: {last_exc}") from last_exc


def _retry_after_seconds(value: str | None) -> float | None:
    """Seconds from a Retry-After header. Only the numeric form; an HTTP-date is ignored."""
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and seconds >= 0 else None


def decode_response(resp: httpx.Response) -> str:
    """Decode response bytes to str, using detected or declared charset."""
    content_type = resp.headers.get("content-type", "")
    encoding: str | None = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            encoding = part.split("=", 1)[1].strip().strip('"')
            break

    if not encoding:
        import chardet
        detected = chardet.detect(resp.content)
        encoding = detected.get("encoding") or "utf-8"

    try:
        return resp.content.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return resp.content.decode("utf-8", errors="replace")
