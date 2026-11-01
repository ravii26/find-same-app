"""Unit tests for Dramatiq actors."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.models import ScanStatus


@pytest.mark.asyncio
async def test_execute_scan_happy_path(tmp_path):
    """_execute_scan writes result and sets completed status."""
    from app.jobs.actors import _execute_scan

    scan_id = "scn_test001"
    request_dict = {
        "brand_name": "Petpooja",
        "android_packages": ["com.petpooja.billing"],
        "keywords": ["petpooja"],
    }

    mock_report = MagicMock()
    mock_report.warnings = []
    mock_report.sources = []
    mock_report.summary.total_results = 0

    with patch("app.jobs.actors.get_settings") as mock_gs, \
         patch("app.jobs.actors.status_store") as mock_ss, \
         patch("app.jobs.actors.scan_log") as mock_log, \
         patch("app.jobs.actors.result_store") as mock_rs, \
         patch("app.jobs.actors.run_scan", new=AsyncMock(return_value=mock_report)), \
         patch("app.jobs.actors._get_redis") as mock_get_redis:

        settings = MagicMock()
        settings.scan_log_path.return_value = tmp_path / "scan.log"
        mock_gs.return_value = settings

        redis_client = AsyncMock()
        mock_get_redis.return_value = redis_client
        mock_ss.set_status = AsyncMock()
        mock_ss.add_to_running = AsyncMock()
        mock_ss.update_meta_field = AsyncMock()
        mock_ss.remove_from_running = AsyncMock()

        mock_log.append_sync = MagicMock()
        mock_rs.write_result = MagicMock()

        await _execute_scan(scan_id, request_dict)

    mock_rs.write_result.assert_called_once_with(mock_report)
    mock_ss.set_status.assert_called()


@pytest.mark.asyncio
async def test_execute_scan_sets_failed_on_exception(tmp_path):
    """If run_scan raises, status is set to failed and exception re-raised."""
    from app.jobs.actors import _execute_scan

    scan_id = "scn_fail001"
    request_dict = {
        "brand_name": "TestBrand",
        "keywords": ["test"],
    }

    with patch("app.jobs.actors.get_settings") as mock_gs, \
         patch("app.jobs.actors.status_store") as mock_ss, \
         patch("app.jobs.actors.scan_log") as mock_log, \
         patch("app.jobs.actors.result_store"), \
         patch("app.jobs.actors.run_scan", new=AsyncMock(side_effect=RuntimeError("scan crashed"))), \
         patch("app.jobs.actors._get_redis") as mock_get_redis:

        settings = MagicMock()
        settings.scan_log_path.return_value = tmp_path / "scan.log"
        mock_gs.return_value = settings

        redis_client = AsyncMock()
        mock_get_redis.return_value = redis_client
        mock_ss.set_status = AsyncMock()
        mock_ss.add_to_running = AsyncMock()
        mock_ss.update_meta_field = AsyncMock()
        mock_ss.remove_from_running = AsyncMock()
        mock_log.append_sync = MagicMock()

        with pytest.raises(RuntimeError, match="scan crashed"):
            await _execute_scan(scan_id, request_dict)

    # Should have been called twice: once for running, once for failed
    statuses = [call.args[2] for call in mock_ss.set_status.call_args_list]
    assert ScanStatus.failed in statuses


@pytest.mark.asyncio
async def test_execute_scan_completed_with_errors_on_warnings(tmp_path):
    """Report with warnings results in completed_with_errors status."""
    from app.jobs.actors import _execute_scan

    scan_id = "scn_warn001"
    request_dict = {
        "brand_name": "TestBrand",
        "keywords": ["test"],
    }

    mock_report = MagicMock()
    mock_report.warnings = ["URLhaus enrichment failed"]
    mock_report.sources = []
    mock_report.summary.total_results = 0

    set_status_calls = []

    async def capture_status(redis, sid, status, **kwargs):
        set_status_calls.append(status)

    with patch("app.jobs.actors.get_settings") as mock_gs, \
         patch("app.jobs.actors.status_store") as mock_ss, \
         patch("app.jobs.actors.scan_log") as mock_log, \
         patch("app.jobs.actors.result_store"), \
         patch("app.jobs.actors.run_scan", new=AsyncMock(return_value=mock_report)), \
         patch("app.jobs.actors._get_redis") as mock_get_redis:

        settings = MagicMock()
        settings.scan_log_path.return_value = tmp_path / "scan.log"
        mock_gs.return_value = settings

        redis_client = AsyncMock()
        mock_get_redis.return_value = redis_client
        mock_ss.set_status = AsyncMock(side_effect=capture_status)
        mock_ss.add_to_running = AsyncMock()
        mock_ss.update_meta_field = AsyncMock()
        mock_ss.remove_from_running = AsyncMock()
        mock_log.append_sync = MagicMock()

        await _execute_scan(scan_id, request_dict)

    assert ScanStatus.completed_with_errors in set_status_calls
