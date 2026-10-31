# app/analysis/apk_analyzer.py
"""
APK static analysis — run in a resource-limited child process.

Never executes the APK. Never stores download URLs.
Fetches into DATA_DIR/tmp/, analyzes, deletes immediately.

The download client re-validates every redirect hop with the SSRF guard. The
parsing itself lives in apk_parser.py, run as python -I apk_parser.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import httpx

from app.api.models import ApkAnalysisResult
from app.config.settings import get_settings
from app.utils.http_client import build_ssrf_client
from app.utils.ssrf_guard import SSRFError, check_url

log = logging.getLogger(__name__)

_PARSER = Path(__file__).with_name("apk_parser.py")
_DL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}


async def analyze_from_url(apk_url: str, official_cert_sha256: str | None) -> ApkAnalysisResult:
    """Download and analyse an APK (see _analyze_from_url); every failure is logged.

    One APK gets at most apk_download_seconds to download plus apk_analysis_wall_seconds to analyse, so a slow
    server cannot hold the whole APK pass. The timed-out file is reported without evidence; the others are kept.
    """
    settings = get_settings()
    limit = settings.apk_download_seconds + settings.apk_analysis_wall_seconds
    try:
        result = await asyncio.wait_for(_analyze_from_url(apk_url, official_cert_sha256), limit)
    except asyncio.TimeoutError:
        result = ApkAnalysisResult(analysis_error=f"download and analysis exceeded {limit}s")
    if result.analysis_error:
        log.warning("apk analysis of %s failed: %s", apk_url, result.analysis_error)
    return result


async def _analyze_from_url(
    apk_url: str,
    official_cert_sha256: str | None,
) -> ApkAnalysisResult:
    """
    Fetch an APK from apk_url (for analysis only — URL not stored),
    analyze it in a resource-limited child process, then delete it.
    Never raises; failures come back as ApkAnalysisResult.analysis_error.
    """
    settings = get_settings()
    tmp_dir = settings.tmp_dir()
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        check_url(apk_url)
    except SSRFError as exc:
        return ApkAnalysisResult(analysis_error=f"SSRF blocked: {exc}")

    cap = settings.max_apk_bytes
    timeout = httpx.Timeout(connect=10, read=120, write=120, pool=5)
    apk_path = tmp_dir / f"apk_{os.urandom(8).hex()}.apk"

    try:
        async with build_ssrf_client(timeout) as client:
            # Cheap size pre-check; servers that reject HEAD fall through to the
            # streaming fetch, which enforces the cap anyway.
            try:
                head = await client.head(apk_url, headers=_DL_HEADERS)
                cl = head.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > cap:
                    return ApkAnalysisResult(analysis_error=f"APK too large: {cl} bytes (max {cap})")
            except SSRFError as exc:
                return ApkAnalysisResult(analysis_error=f"SSRF blocked: {exc}")
            except Exception as exc:  # many servers reject HEAD; the streaming GET below enforces the cap anyway
                log.info("apk: HEAD failed for %s: %s: %s", apk_url, type(exc).__name__, exc)

            total = 0
            async with client.stream("GET", apk_url, headers=_DL_HEADERS) as resp:
                resp.raise_for_status()
                with apk_path.open("wb") as f:
                    async for chunk in resp.aiter_bytes(64 * 1024):
                        total += len(chunk)
                        if total > cap:
                            return ApkAnalysisResult(
                                analysis_error=f"APK exceeds size limit ({cap} bytes)"
                            )
                        f.write(chunk)
        return await _analyze_file(apk_path, official_cert_sha256)
    except SSRFError as exc:
        return ApkAnalysisResult(analysis_error=f"SSRF blocked: {exc}")
    except httpx.HTTPStatusError as exc:
        return ApkAnalysisResult(analysis_error=f"Fetch failed: HTTP {exc.response.status_code}")
    except Exception as exc:
        return ApkAnalysisResult(analysis_error=f"Fetch failed: {exc}")
    finally:
        apk_path.unlink(missing_ok=True)


async def _analyze_file(apk_path: Path, official_cert_sha256: str | None) -> ApkAnalysisResult:
    """Run the parser in a child process with CPU/memory limits and a wall-clock timeout."""
    settings = get_settings()
    env = {
        # Minimal environment: no proxies, no inherited secrets.
        "PATH": os.environ.get("PATH", ""),
        "APK_CPU_SECONDS": str(settings.apk_analysis_cpu_seconds),
        "APK_MEMORY_MB": str(settings.apk_analysis_memory_mb),
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(_PARSER), str(apk_path), official_cert_sha256 or "",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except Exception as exc:
        return ApkAnalysisResult(analysis_error=f"Subprocess error: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=settings.apk_analysis_wall_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ApkAnalysisResult(analysis_error="APK analysis timed out")

    parser_log = stderr.decode("utf-8", errors="replace").strip()
    if parser_log:
        log.info("APK parser output for %s: %s", apk_path.name, parser_log[:2000])
    if proc.returncode != 0:
        return ApkAnalysisResult(analysis_error=f"Parser exited {proc.returncode}: {parser_log[:500]}")

    try:
        return ApkAnalysisResult(**json.loads(stdout.decode()))
    except Exception as exc:
        return ApkAnalysisResult(analysis_error=f"Parser output parse error: {exc}")