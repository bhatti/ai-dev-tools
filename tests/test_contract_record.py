"""Tests for scripts/contract/record.py"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from scripts.contract.record import main as record_main, _write_result


class TestWriteResult:
    def test_writes_expected_fields(self, tmp_path: Path):
        _write_result(tmp_path, test_exit=0, proxy_used=True,
                      service_url="http://localhost:8080", mock_port="8081")
        data = json.loads((tmp_path / "record_result.json").read_text())
        assert data["proxy_used"] is True
        assert data["test_exit_code"] == 0
        assert data["service_url"] == "http://localhost:8080"
        assert data["mock_service_url"] == "http://localhost:8081"

    def test_proxy_used_false(self, tmp_path: Path):
        _write_result(tmp_path, test_exit=0, proxy_used=False,
                      service_url="http://x", mock_port="8081")
        data = json.loads((tmp_path / "record_result.json").read_text())
        assert data["proxy_used"] is False


class TestRecordMain:
    @patch("scripts.contract.record.probe_through_proxy")
    @patch("scripts.contract.record.wait_for_service", return_value=True)
    @patch("scripts.contract.record.load_config")
    @patch("scripts.contract.record.get_workspace_dir")
    def test_writes_record_result_when_no_repo(self, mock_ws, mock_cfg, mock_wait,
                                                mock_probe, tmp_path: Path):
        mock_ws.return_value = tmp_path
        mock_cfg.return_value = {}
        (tmp_path / "recordings").mkdir()

        record_main()

        assert (tmp_path / "record_result.json").exists()
        data = json.loads((tmp_path / "record_result.json").read_text())
        assert data["proxy_used"] is True
        mock_probe.assert_called_once()

    @patch("scripts.contract.record.probe_through_proxy")
    @patch("scripts.contract.record.wait_for_service", return_value=False)
    @patch("scripts.contract.record.load_config")
    @patch("scripts.contract.record.get_workspace_dir")
    def test_writes_result_when_ams_not_ready(self, mock_ws, mock_cfg, mock_wait,
                                               mock_probe, tmp_path: Path):
        mock_ws.return_value = tmp_path
        mock_cfg.return_value = {}

        record_main()

        data = json.loads((tmp_path / "record_result.json").read_text())
        assert data["proxy_used"] is False
        mock_probe.assert_not_called()

    @patch("scripts.contract.record.probe_through_proxy")
    @patch("scripts.contract.record.load_config")
    @patch("scripts.contract.record.get_workspace_dir")
    def test_skips_probe_when_service_not_ready(self, mock_ws, mock_cfg,
                                                mock_probe, tmp_path: Path):
        """AMS ready but service-under-test not reachable → proxy_used=False, no probe."""
        mock_ws.return_value = tmp_path
        mock_cfg.return_value = {}
        # First call (AMS) → True; second call (service) → False
        with patch("scripts.contract.record.wait_for_service", side_effect=[True, False]):
            record_main()

        data = json.loads((tmp_path / "record_result.json").read_text())
        assert data["proxy_used"] is False
        mock_probe.assert_not_called()

    @patch("scripts.contract.record.probe_through_proxy")
    @patch("scripts.contract.record.subprocess")
    @patch("scripts.contract.record.wait_for_service", return_value=True)
    @patch("scripts.contract.record.load_config")
    @patch("scripts.contract.record.get_workspace_dir")
    def test_runs_make_test_when_repo_exists(self, mock_ws, mock_cfg, mock_wait,
                                              mock_sub, mock_probe, tmp_path: Path):
        mock_ws.return_value = tmp_path
        mock_cfg.return_value = {}
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (tmp_path / "recordings").mkdir()
        mock_sub.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired

        record_main()

        mock_sub.run.assert_called_once()
        args = mock_sub.run.call_args[0][0]
        assert args == ["make", "test"]

    @patch("scripts.contract.record.probe_through_proxy")
    @patch("scripts.contract.record.subprocess")
    @patch("scripts.contract.record.wait_for_service", return_value=True)
    @patch("scripts.contract.record.load_config")
    @patch("scripts.contract.record.get_workspace_dir")
    def test_handles_make_test_timeout(self, mock_ws, mock_cfg, mock_wait,
                                       mock_sub, mock_probe, tmp_path: Path):
        mock_ws.return_value = tmp_path
        mock_cfg.return_value = {}
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (tmp_path / "recordings").mkdir()
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        mock_sub.run.side_effect = subprocess.TimeoutExpired(["make"], 1800)

        record_main()

        data = json.loads((tmp_path / "record_result.json").read_text())
        assert data["test_exit_code"] == 1  # timeout → non-zero exit
        assert data["proxy_used"] is True
