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
        # No recordings → 0 endpoints → 0 iterations; AMS not used but all files written.
        main()

        assert (tmp_path / "fuzz_result.json").exists()
        assert (tmp_path / "fuzz_results.xml").exists()
        assert (tmp_path / "contract_test_summary.json").exists()
        data = json.loads((tmp_path / "fuzz_result.json").read_text())
        assert data["ams_used"] is False
        assert data["iterations"] == 0

    @patch("scripts.contract.fuzz.run_security_probes",
           return_value=[{"probe": "sqli:/api/items", "severity": "medium"}])
    @patch("scripts.contract.fuzz.discover_endpoints",
           return_value=[("GET", "/api/items")])
    @patch("scripts.contract.fuzz.wait_for_service", return_value=False)
    @patch("scripts.contract.fuzz.load_config")
    @patch("scripts.contract.fuzz.get_workspace_dir")
    def test_runs_probes_when_ams_not_ready_but_recordings_exist(
            self, mock_ws, mock_cfg, mock_wait, mock_discover, mock_probe, tmp_path: Path):
        """Security probes must run even without AMS so findings are always collected."""
        mock_ws.return_value = tmp_path
        mock_cfg.return_value = {}
        (tmp_path / "recordings").mkdir()

        main()

        data = json.loads((tmp_path / "fuzz_result.json").read_text())
        assert data["ams_used"] is False
        assert data["iterations"] == 2  # 1 GET endpoint × 2 probes
        assert len(data["findings"]) == 1
        summary = json.loads((tmp_path / "contract_test_summary.json").read_text())
        assert summary["fuzz_iterations"] == 2
        assert summary["fuzz_findings"] == 1
        assert summary["contract_breaking_changes"] == 0  # no AMS → replay skipped

    @patch("scripts.contract.fuzz.run_security_probes",
           return_value=[{"probe": "sqli:/actuator/env", "severity": "critical",
                          "sqli_leak": False, "stack_leak": False, "cred_leak": True}])
    @patch("scripts.contract.fuzz.discover_endpoints",
           return_value=[("GET", "/actuator/env"), ("GET", "/api/users")])
    @patch("scripts.contract.fuzz._run_contract_replay", return_value={"succeeded": 5, "failed": 0})
    @patch("scripts.contract.fuzz._upload_openapi_spec")
    @patch("scripts.contract.fuzz.wait_for_service", return_value=True)
    @patch("scripts.contract.fuzz.load_config")
    @patch("scripts.contract.fuzz.get_workspace_dir")
    def test_enriched_summary_with_cred_leak(self, mock_ws, mock_cfg, mock_wait, mock_upload,
                                              mock_replay, mock_discover, mock_probe, tmp_path: Path):
        mock_ws.return_value = tmp_path
        mock_cfg.return_value = {}
        (tmp_path / "recordings").mkdir()

        main()

        summary = json.loads((tmp_path / "contract_test_summary.json").read_text())
        assert summary["status"] == "FAIL"
        assert summary["critical_findings"] == 1
        assert summary["endpoints_scanned"] == 2
        assert "SQLi" in summary["probe_types"]
        assert "credential-exposure" in summary["probe_types"]

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

    @patch("scripts.contract.fuzz.run_security_probes", return_value=[
        {"probe": "sqli:/api/items?q=' OR 1=1--", "status": 200, "severity": "critical",
         "sqli_leak": True, "stack_leak": False, "cred_leak": False},
    ])
    @patch("scripts.contract.fuzz.discover_endpoints",
           return_value=[("GET", "/api/items"), ("POST", "/api/orders")])
    @patch("scripts.contract.fuzz._run_contract_replay", return_value={"succeeded": 4, "failed": 1})
    @patch("scripts.contract.fuzz._upload_openapi_spec")
    @patch("scripts.contract.fuzz.wait_for_service", return_value=True)
    @patch("scripts.contract.fuzz.load_config")
    @patch("scripts.contract.fuzz.get_workspace_dir")
    def test_sqli_finding_produces_correct_output_structure(
            self, mock_ws, mock_cfg, mock_wait, mock_upload, mock_replay, mock_discover,
            mock_probe, tmp_path: Path):
        """SQLi finding must appear in fuzz_result.json, summary, and JUnit XML with correct fields."""
        mock_ws.return_value = tmp_path
        mock_cfg.return_value = {}
        (tmp_path / "recordings").mkdir()

        main()

        # fuzz_result.json: full finding detail preserved
        fuzz = json.loads((tmp_path / "fuzz_result.json").read_text())
        assert fuzz["iterations"] == 1 * 2 + 1 * 3  # 1 GET×2 + 1 POST×3 = 5
        assert fuzz["ams_used"] is True
        assert len(fuzz["findings"]) == 1
        finding = fuzz["findings"][0]
        assert finding["severity"] == "critical"
        assert finding["sqli_leak"] is True
        assert finding["stack_leak"] is False
        assert finding["cred_leak"] is False

        # contract_test_summary.json: aggregates and probe_types
        summary = json.loads((tmp_path / "contract_test_summary.json").read_text())
        assert summary["status"] == "FAIL"
        assert summary["critical_findings"] == 1
        assert summary["fuzz_findings"] == 1
        assert summary["contract_breaking_changes"] == 1
        assert summary["endpoints_scanned"] == 2
        assert "SQLi" in summary["probe_types"]
        assert "XSS" in summary["probe_types"]          # POST endpoint triggers write probes
        assert "oversized-payload" in summary["probe_types"]

        # JUnit XML: failures count matches finding count
        xml = (tmp_path / "fuzz_results.xml").read_text()
        assert 'failures="1"' in xml
        assert "sqli" in xml.lower()

    @patch("scripts.contract.fuzz.run_security_probes", return_value=[
        {"probe": "sqli:/health", "status": 500, "severity": "critical",
         "sqli_leak": False, "stack_leak": True, "cred_leak": False},
        {"probe": "path-trav:/health", "status": 500, "severity": "medium",
         "sqli_leak": False, "stack_leak": False, "cred_leak": False},
    ])
    @patch("scripts.contract.fuzz.discover_endpoints",
           return_value=[("GET", "/health"), ("GET", "/api/users")])
    @patch("scripts.contract.fuzz._run_contract_replay", return_value={"succeeded": 2, "failed": 0})
    @patch("scripts.contract.fuzz._upload_openapi_spec")
    @patch("scripts.contract.fuzz.wait_for_service", return_value=True)
    @patch("scripts.contract.fuzz.load_config")
    @patch("scripts.contract.fuzz.get_workspace_dir")
    def test_stack_trace_finding_is_critical_in_output(
            self, mock_ws, mock_cfg, mock_wait, mock_upload, mock_replay, mock_discover,
            mock_probe, tmp_path: Path):
        """Java stack trace leak must produce a critical finding with stack_leak=True."""
        mock_ws.return_value = tmp_path
        mock_cfg.return_value = {}
        (tmp_path / "recordings").mkdir()

        main()

        fuzz = json.loads((tmp_path / "fuzz_result.json").read_text())
        assert fuzz["iterations"] == 4  # 2 GET endpoints × 2 probes
        critical = [f for f in fuzz["findings"] if f["severity"] == "critical"]
        assert len(critical) == 1
        assert critical[0]["stack_leak"] is True

        summary = json.loads((tmp_path / "contract_test_summary.json").read_text())
        assert summary["status"] == "FAIL"
        assert summary["critical_findings"] == 1
        assert summary["fuzz_findings"] == 2    # both findings captured
        assert summary["endpoints_scanned"] == 2

        xml = (tmp_path / "fuzz_results.xml").read_text()
        assert 'failures="2"' in xml
        assert 'tests="4"' in xml
