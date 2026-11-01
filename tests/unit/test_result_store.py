"""Unit tests for atomic result store and file operations."""

from __future__ import annotations

import datetime
from pathlib import Path


from app.api.models import (
    ScanQuery,
    ScanReport,
    ScanSummary,
)
from app.storage.result_store import (
    delete_scan,
    ensure_scan_dir,
    read_result,
    write_result,
)


def _make_report(scan_id: str = "scn_test123") -> ScanReport:
    return ScanReport(
        scan_id=scan_id,
        query=ScanQuery(
            brand_name="Petpooja",
            keywords=["petpooja"],
            received_at=datetime.datetime.now(datetime.UTC),
        ),
        summary=ScanSummary(total_results=0, scoring_ruleset="v1"),
    )


def test_write_and_read_result(tmp_path: Path, monkeypatch):
    from app.config import settings as settings_module
    s = settings_module.get_settings()
    monkeypatch.setattr(s, "data_dir", tmp_path / "data")
    (tmp_path / "data" / "results").mkdir(parents=True)

    report = _make_report()
    ensure_scan_dir(report.scan_id)
    write_result(report)
    assert read_result(report.scan_id) is not None
    loaded = read_result(report.scan_id)
    assert loaded is not None
    assert loaded.scan_id == report.scan_id


def test_write_is_atomic(tmp_path: Path, monkeypatch):
    from app.config import settings as settings_module
    s = settings_module.get_settings()
    monkeypatch.setattr(s, "data_dir", tmp_path / "data")
    (tmp_path / "data" / "results").mkdir(parents=True)

    report = _make_report()
    ensure_scan_dir(report.scan_id)
    write_result(report)

    # .tmp file should be gone after write
    result_dir = s.scan_result_dir(report.scan_id)
    tmp_files = list(result_dir.glob("*.tmp"))
    assert tmp_files == [], "Temporary file left after atomic write"


def test_delete_scan(tmp_path: Path, monkeypatch):
    from app.config import settings as settings_module
    s = settings_module.get_settings()
    monkeypatch.setattr(s, "data_dir", tmp_path / "data")
    (tmp_path / "data" / "results").mkdir(parents=True)

    report = _make_report()
    ensure_scan_dir(report.scan_id)
    write_result(report)
    assert read_result(report.scan_id) is not None

    deleted = delete_scan(report.scan_id)
    assert deleted is True
    assert read_result(report.scan_id) is None


def test_delete_nonexistent_returns_false(tmp_path: Path, monkeypatch):
    from app.config import settings as settings_module
    s = settings_module.get_settings()
    monkeypatch.setattr(s, "data_dir", tmp_path / "data")
    (tmp_path / "data" / "results").mkdir(parents=True)

    assert delete_scan("scn_doesnotexist") is False


def test_read_corrupt_returns_none(tmp_path: Path, monkeypatch):
    from app.config import settings as settings_module
    s = settings_module.get_settings()
    monkeypatch.setattr(s, "data_dir", tmp_path / "data")
    (tmp_path / "data" / "results").mkdir(parents=True)

    scan_id = "scn_corrupt"
    ensure_scan_dir(scan_id)
    out = s.scan_result_dir(scan_id) / "output.json"
    out.write_text("not valid json}")

    result = read_result(scan_id)
    assert result is None
    # Corrupt file should be quarantined
    quarantine = out.with_suffix(".corrupt")
    assert quarantine.exists()
