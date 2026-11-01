# app/storage/scan_log.py
"""Append-only scan log — written to DATA_DIR/results/{scan_id}/scan.log."""

from __future__ import annotations

import contextvars
import datetime
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _ts() -> str:
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_sync(log_path: Path, scan_id: str, level: str, message: str) -> None:
    """Append one line. Format: ISO-8601 [scan_id] [LEVEL] message."""
    line = f"{_ts()} [{scan_id}] [{level:<5}] {message}\n"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


# The scan whose log receives records from the current context. Set by capture(); tasks and to_thread() calls
# made inside the scan inherit it, so every log record from that scan lands in its scan.log.
_capturing: contextvars.ContextVar[tuple[str, Path] | None] = contextvars.ContextVar("scan_capture", default=None)
_write_lock = threading.Lock()


class _ScanLogHandler(logging.Handler):
    """Writes each log record emitted during a scan into that scan's scan.log."""

    def emit(self, record: logging.LogRecord) -> None:
        target = _capturing.get()
        if target is None:
            return
        scan_id, path = target
        try:
            message = record.getMessage()
            if record.exc_info:
                message += " | " + logging.Formatter().formatException(record.exc_info).replace("\n", " | ")
            line = f"{_ts()} [{scan_id}] [{record.levelname:<5}] [{record.name}] {message}\n"
            with _write_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            self.handleError(record)


_handler = _ScanLogHandler()


def _install() -> None:
    root = logging.getLogger()
    if _handler not in root.handlers:
        root.addHandler(_handler)


@contextmanager
def capture(scan_id: str, log_path: Path) -> Iterator[None]:
    """Route every log record emitted inside this block (in this task and its children) to the scan's log."""
    _install()
    token = _capturing.set((scan_id, log_path))
    try:
        yield
    finally:
        _capturing.reset(token)
