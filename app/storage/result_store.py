# app/storage/result_store.py
"""
Atomic result file store — write-once, permanent JSON files on disk.

write path: DATA_DIR/results/{scan_id}/output.json
atomic:     write to output.tmp → fsync → os.replace to output.json
delete:     remove the entire {scan_id}/ directory
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path

from app.api.models import ScanReport
from app.config.settings import get_settings

# Scan ids are "scn_" + a ULID. Anything else — "..", "a/b", "scn_x/.." — must never reach the filesystem:
# the id becomes a directory name that delete_scan() removes recursively.
log = logging.getLogger(__name__)

_SCAN_ID_RE = re.compile(r"^scn_[A-Za-z0-9]{1,64}$")


class InvalidScanId(ValueError):
    """The scan id is not a well-formed scan identifier."""


def is_valid_scan_id(scan_id: object) -> bool:
    return isinstance(scan_id, str) and _SCAN_ID_RE.fullmatch(scan_id) is not None


def _result_dir(scan_id: str) -> Path:
    """The scan's directory. Raises InvalidScanId rather than ever returning a path outside results/."""
    if not is_valid_scan_id(scan_id):
        raise InvalidScanId(f"invalid scan id: {scan_id!r}")
    root = get_settings().results_dir().resolve()
    path = get_settings().scan_result_dir(scan_id)
    # Belt and braces: even a well-formed id must resolve to a direct child of results/ (no symlink escapes)
    if path.resolve().parent != root:
        raise InvalidScanId(f"scan id escapes the results directory: {scan_id!r}")
    return path


def _output_path(scan_id: str) -> Path:
    return _result_dir(scan_id) / "output.json"


def _tmp_path(scan_id: str) -> Path:
    return _output_path(scan_id).with_suffix(".tmp")


def ensure_scan_dir(scan_id: str) -> Path:
    """Create the scan directory and return it. Called before enqueuing."""
    d = _result_dir(scan_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_result(report: ScanReport) -> None:
    """Atomically write the full scan report to disk. Never half-written."""
    out = _output_path(report.scan_id)
    tmp = _tmp_path(report.scan_id)
    out.parent.mkdir(parents=True, exist_ok=True)

    payload = report.model_dump_json(indent=2)
    with tmp.open("w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp, out)


def read_result(scan_id: str) -> ScanReport | None:
    """
    Read the result JSON if it exists and parse it.

    Returns None if the file is missing. Quarantines corrupt files.
    """
    path = _output_path(scan_id)
    if not path.exists():
        return None
    try:
        data = path.read_text(encoding="utf-8")
        return ScanReport.model_validate_json(data)
    except Exception as exc:
        log.error("report %s is unreadable (%s: %s); moved aside as .corrupt", path, type(exc).__name__, exc)
        _quarantine(path)
        return None


def delete_scan(scan_id: str) -> bool:
    """Remove the entire scan directory. Returns True if it existed."""
    d = _result_dir(scan_id)
    if d.exists():
        shutil.rmtree(d)
        return True
    return False


def _quarantine(path: Path) -> None:
    quarantine = path.with_suffix(".corrupt")
    try:
        os.replace(path, quarantine)
    except OSError as exc:
        log.error("could not quarantine %s: %s", path, exc)
