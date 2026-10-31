# app/analysis/apk_parser.py
"""Standalone APK parser — executed in a child process by apk_analyzer.

    python -I apk_parser.py <apk_path> [official_cert_fingerprint]

Prints one JSON object on stdout. Imports nothing from `app` so the child
stays light and cannot be influenced by project modules. Never executes the
APK. Resource limits (env APK_CPU_SECONDS / APK_MEMORY_MB) and a socket block
are applied inside the child itself.

Parsing uses androguard (binary manifest, v1/v2/v3 signature blocks). If it is
missing or fails, a best-effort fallback reads the v1 signature file and greps
the manifest; `parser` in the output says which path produced the result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import zipfile

MAX_UNCOMPRESSED = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 20_000
MAX_URL_FILE_BYTES = 20 * 1024 * 1024
MAX_URL_SCAN_BYTES = 200 * 1024 * 1024
MAX_PERMISSIONS = 300
MAX_URLS = 50

_URL_RE = re.compile(rb"https?://[^\x00-\x1f\x7f-\xff\s<>\"'`{}|\\^\[\]]+")
_PERM_RE = re.compile(rb"android\.permission\.[A-Za-z_]+")


def apply_limits() -> None:
    """CPU/memory limits and no sockets, set in the child. Best effort per platform."""
    try:
        import resource

        cpu = int(os.environ.get("APK_CPU_SECONDS", "0"))
        if cpu:
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        mem = int(os.environ.get("APK_MEMORY_MB", "0")) * 1024 * 1024
        if mem:
            try:
                resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
            except (ValueError, OSError):
                pass  # not enforceable on every platform (e.g. macOS)
    except ImportError:
        pass

    import socket

    class _NoSocket(socket.socket):
        # A subclass (not a function) so modules that subclass socket.socket at
        # import time, like ssl, still import fine.
        def __init__(self, *_a, **_k):
            raise OSError("network access is disabled during APK analysis")

    socket.socket = _NoSocket  # type: ignore[misc]


def normalize_fp(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.replace(":", "").replace(" ", "").strip().upper()
    return cleaned or None


def cert_match(official: str | None, certs_der: list[bytes]) -> bool | None:
    """True if any signing cert matches; None when not comparable or no certs."""
    off = normalize_fp(official)
    if not off or not certs_der:
        return None
    algo = {40: hashlib.sha1, 64: hashlib.sha256}.get(len(off))
    if algo is None:
        return None
    return any(algo(c).hexdigest().upper() == off for c in certs_der)


def file_hashes(path: str) -> tuple[str, str]:
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(chunk)
            md5.update(chunk)
    return sha.hexdigest(), md5.hexdigest()


def check_zip(z: zipfile.ZipFile) -> list[str]:
    infos = z.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ValueError(f"too many archive entries ({len(infos)})")
    for i in infos:
        if ".." in i.filename.split("/") or i.filename.startswith("/"):
            raise ValueError(f"suspicious ZIP entry: {i.filename}")
    if sum(i.file_size for i in infos) > MAX_UNCOMPRESSED:
        raise ValueError("ZIP bomb detected")
    return [i.filename for i in infos]


def embedded_urls(z: zipfile.ZipFile) -> list[str]:
    urls: set[str] = set()
    scanned = 0
    for info in z.infolist():
        if info.is_dir() or info.file_size > MAX_URL_FILE_BYTES:
            continue
        if scanned + info.file_size > MAX_URL_SCAN_BYTES or len(urls) >= MAX_URLS * 2:
            break
        scanned += info.file_size
        try:
            raw = z.read(info)
        except Exception:
            continue
        for m in _URL_RE.finditer(raw):
            u = m.group(0).decode("utf-8", errors="replace").rstrip(".,);")
            if len(u) < 200:
                urls.add(u)
    return sorted(urls)[:MAX_URLS]


def _label(apk) -> str | None:
    try:
        return (apk.get_app_name() or None)
    except Exception:  # label comes from resources; absent/obfuscated ones must not fail the parse
        return None


def _parse_androguard(path: str) -> dict:
    from loguru import logger

    logger.remove()  # androguard logs every manifest tag at DEBUG
    from androguard.core.apk import APK

    apk = APK(path)
    # Every signer certificate from every scheme (v3/v2 first, then the legacy v1 block). An APK can carry
    # more than one (key rotation), and a match against any of them means the official key signed it.
    certs: list[bytes] = []
    for getter in (apk.get_certificates_der_v3, apk.get_certificates_der_v2):
        try:
            certs.extend(getter())
        except Exception:
            pass
    try:
        certs.extend(c.dump() for c in apk.get_certificates_v1())
    except Exception:
        pass
    certs = list(dict.fromkeys(certs))  # de-duplicate, keep order
    return {
        "parser": "androguard",
        "package_name": apk.get_package(),
        "app_label": _label(apk),
        "version": apk.get_androidversion_name(),
        "permissions": sorted(set(apk.get_permissions()))[:MAX_PERMISSIONS],
        "_certs": certs,
    }


def _parse_fallback(path: str, z: zipfile.ZipFile, names: list[str]) -> dict:
    out: dict = {"parser": "fallback", "permissions": [], "_certs": []}
    if "AndroidManifest.xml" in names:
        raw = z.read("AndroidManifest.xml")
        out["permissions"] = sorted({m.group(0).decode() for m in _PERM_RE.finditer(raw)})[:MAX_PERMISSIONS]
    sig_files = [n for n in names if re.match(r"META-INF/.*\.(RSA|DSA|EC)$", n, re.I)]
    if sig_files:
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.serialization import pkcs7

            certs = pkcs7.load_der_pkcs7_certificates(z.read(sig_files[0]))
            out["_certs"] = [c.public_bytes(serialization.Encoding.DER) for c in certs[:1]]
        except Exception:
            pass
    return out


def analyze(path: str, official_cert: str | None) -> dict:
    result: dict = {}
    try:
        result["sha256"], result["md5"] = file_hashes(path)
        with zipfile.ZipFile(path, "r") as z:
            names = check_zip(z)
            try:
                parsed = _parse_androguard(path)
            except Exception as exc:
                parsed = _parse_fallback(path, z, names)
                parsed["parse_note"] = f"androguard failed: {type(exc).__name__}: {exc}"[:300]
            certs = parsed.pop("_certs")
            note = parsed.pop("parse_note", None)
            result.update(parsed)
            if certs:
                result["cert_sha256"] = hashlib.sha256(certs[0]).hexdigest().upper()
                result["cert_sha1"] = hashlib.sha1(certs[0]).hexdigest().upper()
                m = cert_match(official_cert, certs)
                if m is not None:
                    result["cert_match"] = m
            result["embedded_urls"] = embedded_urls(z)
            if note and not result.get("package_name"):
                result["analysis_error"] = note
    except Exception as exc:
        result["analysis_error"] = f"{type(exc).__name__}: {exc}"[:500]
    return result


def main(argv: list[str]) -> int:
    # The parent process reads stdout as the JSON result, so the result is written raw, not as a log line.
    # Logs go to stderr, which the parent keeps and records in the scan log.
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="[%(levelname)s] [apk_parser] %(message)s")
    log = logging.getLogger("apk_parser")
    if len(argv) < 2:
        log.error("usage: apk_parser.py <apk> [official_cert]")
        return 2
    apply_limits()
    sys.stdout.write(json.dumps(analyze(argv[1], argv[2] if len(argv) > 2 else None)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))