"""Tests for scripts/contract/fuzz.py"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.contract.fuzz import main, _run_contract_replay, _write_junit


class TestRunContractReplay:
    def test_returns_succeeded_failed(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"succeeded": 5, "failed": 1}).encode()
        with patch("scripts.contract.fuzz.urllib.request.urlopen", return_value=mock_resp):
            result = _run_contract_replay("http://localhost:8081", "http://localhost:8080")
        assert result["succeeded"] == 5
        assert result["failed"] == 1

    def test_handles_http_error(self):
        import urllib.error
        err = urllib.error.HTTPError(None, 404, "Not Found", {}, None)
        err.read = lambda: b"not found"
        with patch("scripts.contract.fuzz.urllib.request.urlopen", side_effect=err):
            result = _run_contract_replay("http://localhost:8081", "http://localhost:8080")
        assert result["succeeded"] == 0
        assert "error" in result

    def test_handles_connection_error(self):
        with patch("scripts.contract.fuzz.urllib.request.urlopen", side_effect=OSError("refused")):
            result = _run_contract_replay("http://localhost:8081", "http://localhost:8080")
        assert result["succeeded"] == 0
        assert "error" in result


class TestWriteJunit:
    def test_writes_valid_xml(self, tmp_path: Path):
        findings = [{"probe": "sqli:/todos", "severity": "medium"}]
        _write_junit(tmp_path, findings, iterations=10)
        xml = (tmp_path / "fuzz_results.xml").read_bytes()
        assert b"testsuite" in xml
        assert b"failures" in xml
        assert b"10" in xml  # tests attribute

    def test_empty_findings_writes_clean_xml(self, tmp_path: Path):
        _write_junit(tmp_path, [], iterations=5)
        xml = (tmp_path / "fuzz_results.xml").read_bytes()
        assert b'failures="0"' in xml


class TestFuzzMain:
    @patch("scripts.contract.fuzz.run_security_probes", return_value=[])
    @patch("scripts.contract.fuzz.discover_endpoints", return_value=[("GET", "/todos")])
    @patch("scripts.contract.fuzz._run_contract_replay", return_value={"succeeded": 3, "failed": 0})
    @patch("scripts.contract.fuzz._upload_openapi_spec")
    @patch("scripts.contract.fuzz.wait_for_service", return_value=True)
    @patch("scripts.contract.fuzz.load_config")
    @patch("scripts.contract.fuzz.get_workspace_dir")
    def test_writes_all_artifacts(self, mock_ws, mock_cfg, mock_wait, mock_upload,
                                   mock_replay, mock_discover, mock_probe, tmp_path: Path):
        mock_ws.return_value = tmp_path
        mock_cfg.return_value = {}
        (tmp_path / "recordings").mkdir()

        main()

        assert (tmp_path / "fuzz_result.json").exists()
        assert (tmp_path / "fuzz_results.xml").exists()
        assert (tmp_path / "contract_test_summary.json").exists()

        summary = json.loads((tmp_path / "contract_test_summary.json").read_text())
        assert summary["status"] == "PASS"
        assert summary["fuzz_findings"] == 0

    @patch("scripts.contract.fuzz.run_security_probes",
           return_value=[{"probe": "sqli:/x", "severity": "critical"}])
    @patch("scripts.contract.fuzz.discover_endpoints", return_value=[("GET", "/x")])
    @patch("scripts.contract.fuzz._run_contract_replay", return_value={"succeeded": 0, "failed": 2})
    @patch("scripts.contract.fuzz._upload_openapi_spec")
    @patch("scripts.contract.fuzz.wait_for_service", return_value=True)
    @patch("scripts.contract.fuzz.load_config")
    @patch("scripts.contract.fuzz.get_workspace_dir")
    def test_fail_status_on_critical_findings(self, mock_ws, mock_cfg, mock_wait, mock_upload,
                                               mock_replay, mock_discover, mock_probe, tmp_path: Path):
        mock_ws.return_value = tmp_path
        mock_cfg.return_value = {}
        (tmp_path / "recordings").mkdir()

        main()

        summary = json.loads((tmp_path / "contract_test_summary.json").read_text())
        assert summary["status"] == "FAIL"
        assert summary["critical_findings"] == 1
        assert summary["contract_breaking_changes"] == 2

    @patch("scripts.contract.fuzz.wait_for_service", return_value=False)
    @patch("scripts.contract.fuzz.load_config")
    @patch("scripts.contract.fuzz.get_workspace_dir")
    def test_writes_artifacts_when_ams_not_ready(self, mock_ws, mock_cfg, mock_wait, tmp_path: Path):
        mock_ws.return_value = tmp_path
        mock_cfg.return_value = {}

        main()

        # Still writes all three artifacts even without AMS
        assert (tmp_path / "fuzz_result.json").exists()
        assert (tmp_path / "fuzz_results.xml").exists()
        assert (tmp_path / "contract_test_summary.json").exists()
        data = json.loads((tmp_path / "fuzz_result.json").read_text())
        assert data["ams_used"] is False

    @patch("scripts.contract.fuzz.run_security_probes", return_value=[])
    @patch("scripts.contract.fuzz.discover_endpoints",
           return_value=[("GET", f"/e{i}") for i in range(15)])
    @patch("scripts.contract.fuzz._run_contract_replay", return_value={})
    @patch("scripts.contract.fuzz._upload_openapi_spec")
    @patch("scripts.contract.fuzz.wait_for_service", return_value=True)
    @patch("scripts.contract.fuzz.load_config")
    @patch("scripts.contract.fuzz.get_workspace_dir")
    def test_iterations_calculated_correctly_for_get(
            self, mock_ws, mock_cfg, mock_wait, mock_upload, mock_replay, mock_discover,
            mock_probe, tmp_path: Path):
        mock_ws.return_value = tmp_path
        mock_cfg.return_value = {}
        (tmp_path / "recordings").mkdir()

        main()

        fuzz = json.loads((tmp_path / "fuzz_result.json").read_text())
        # 15 GET endpoints × 2 probes each = 30
        assert fuzz["iterations"] == 30
