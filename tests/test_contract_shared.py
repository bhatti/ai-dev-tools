"""Tests for scripts/contract/_shared.py"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.contract._shared import (
    discover_endpoints,
    probe_through_proxy,
    run_security_probes,
    wait_for_service,
)


class TestWaitForService:
    def test_returns_true_on_200(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        with patch("scripts.contract._shared.urllib.request.urlopen", return_value=mock_resp):
            assert wait_for_service("http://localhost:8080/_health", "svc") is True

    def test_returns_true_on_404(self):
        import urllib.error
        err = urllib.error.HTTPError(None, 404, "Not Found", {}, None)
        err.read = lambda: b""
        with patch("scripts.contract._shared.urllib.request.urlopen", side_effect=err):
            assert wait_for_service("http://localhost:8080/_health", "svc") is True

    def test_returns_false_on_500_exhausted(self):
        import urllib.error
        err = urllib.error.HTTPError(None, 500, "Server Error", {}, None)
        err.read = lambda: b""
        with patch("scripts.contract._shared.urllib.request.urlopen", side_effect=err):
            assert wait_for_service("http://localhost:8080/_health", "svc", retries=2, delay=0) is False

    def test_retries_on_connection_error(self):
        call_count = 0

        def side_effect(*_a, **_kw):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise OSError("connection refused")
            m = MagicMock()
            m.status = 200
            return m

        with patch("scripts.contract._shared.urllib.request.urlopen", side_effect=side_effect):
            assert wait_for_service("http://x", "svc", retries=5, delay=0) is True
        assert call_count == 3

    def test_does_not_mutate_os_environ(self):
        original_no_proxy = os.environ.get("no_proxy")
        with patch("scripts.contract._shared.urllib.request.urlopen", side_effect=OSError()):
            wait_for_service("http://x", "svc", retries=1, delay=0)
        assert os.environ.get("no_proxy") == original_no_proxy


class TestProbeThoughProxy:
    def test_does_not_mutate_os_environ(self):
        """probe_through_proxy must not modify global no_proxy/NO_PROXY."""
        os.environ["no_proxy"] = "original"
        os.environ["NO_PROXY"] = "ORIGINAL"
        with patch("scripts.contract._shared.urllib.request.build_opener") as mock_opener:
            mock_opener.return_value.open.side_effect = OSError()
            probe_through_proxy("http://localhost:8080", "http://localhost:8082")
        assert os.environ.get("no_proxy") == "original"
        assert os.environ.get("NO_PROXY") == "ORIGINAL"
        # cleanup
        os.environ.pop("no_proxy", None)
        os.environ.pop("NO_PROXY", None)

    def test_uses_explicit_proxy_handler(self):
        with patch("scripts.contract._shared.urllib.request.build_opener") as mock_opener, \
             patch("scripts.contract._shared.urllib.request.ProxyHandler") as mock_ph:
            mock_opener.return_value.open.side_effect = OSError()
            probe_through_proxy("http://localhost:8080", "http://proxy:8082")
        mock_ph.assert_called_once_with({"http": "http://proxy:8082", "https": "http://proxy:8082"})


class TestDiscoverEndpoints:
    def test_parses_recording_tree(self, tmp_path: Path):
        # api_contracts/api/Challenges/GET/Recorded1.yaml
        challenges_dir = tmp_path / "api_contracts" / "api" / "Challenges" / "GET"
        challenges_dir.mkdir(parents=True)
        (challenges_dir / "Recorded1.yaml").write_text("resp: 200")

        health_dir = tmp_path / "api_contracts" / "health" / "GET"
        health_dir.mkdir(parents=True)
        (health_dir / "Recorded1.yaml").write_text("resp: 200")

        endpoints = discover_endpoints(tmp_path)
        assert ("GET", "/api/Challenges") in endpoints
        assert ("GET", "/health") in endpoints

    def test_deduplicates_multiple_recordings(self, tmp_path: Path):
        d = tmp_path / "api_contracts" / "todos" / "GET"
        d.mkdir(parents=True)
        (d / "Recorded1.yaml").write_text("")
        (d / "Recorded2.yaml").write_text("")
        endpoints = discover_endpoints(tmp_path)
        assert endpoints.count(("GET", "/todos")) == 1

    def test_empty_dir_returns_empty(self, tmp_path: Path):
        assert discover_endpoints(tmp_path) == []

    def test_skips_non_method_leaf(self, tmp_path: Path):
        # Directory leaf "data" is not an HTTP method — should be skipped
        d = tmp_path / "api_contracts" / "something" / "data"
        d.mkdir(parents=True)
        (d / "Recorded1.yaml").write_text("")
        assert discover_endpoints(tmp_path) == []

    def test_skips_uppercase_path_segment_that_is_not_http_method(self, tmp_path: Path):
        # Path segments like "API", "AUTH", "BATCH" are all-uppercase alpha but not HTTP verbs.
        # Regression test: old code used isalpha()+upper() which matched these as methods.
        for segment in ("API", "AUTH", "BATCH", "V1"):
            d = tmp_path / "api_contracts" / segment / "items"
            d.mkdir(parents=True)
            (d / "Recorded1.yaml").write_text("")
        assert discover_endpoints(tmp_path) == []


class TestRunSecurityProbes:
    def _make_response(self, status: int, body: bytes = b"ok"):
        import urllib.error
        if status >= 400:
            e = urllib.error.HTTPError(None, status, "err", {}, None)
            e.read = lambda: body
            return e
        m = MagicMock()
        m.status = status
        m.read.return_value = body
        return m

    def test_no_findings_on_404(self):
        resp = self._make_response(404)
        with patch("scripts.contract._shared.urllib.request.urlopen", side_effect=resp):
            findings = run_security_probes("http://localhost:8080", [("GET", "/todos")])
        assert findings == []

    def test_finding_on_500(self):
        resp = self._make_response(500, b"Internal Server Error")
        with patch("scripts.contract._shared.urllib.request.urlopen", side_effect=resp):
            findings = run_security_probes("http://localhost:8080", [("GET", "/todos")])
        assert len(findings) == 2  # sqli and path_trav probes, both 500
        assert all(f["severity"] == "medium" for f in findings)

    def test_critical_finding_on_sql_leak(self):
        resp = self._make_response(200, b"You have an error in your SQL syntax")
        with patch("scripts.contract._shared.urllib.request.urlopen", return_value=resp):
            findings = run_security_probes("http://localhost:8080", [("GET", "/q")])
        assert any(f["severity"] == "critical" for f in findings)

    def test_caps_at_20_endpoints(self):
        endpoints = [("GET", f"/e{i}") for i in range(25)]
        with patch("scripts.contract._shared.urllib.request.urlopen", side_effect=OSError()):
            findings = run_security_probes("http://localhost:8080", endpoints)
        # 20 endpoints × 2 GET probes = 40 probes, all OSError → 0 findings
        assert findings == []

    def test_critical_finding_on_credential_exposure(self):
        """Actuator endpoint returning Spring env dump is a critical info-disclosure finding."""
        body = b'{"propertySources":[{"properties":{"spring.datasource.password":{"value":"s3cr3t"}}}]}'
        resp = self._make_response(200, body)
        with patch("scripts.contract._shared.urllib.request.urlopen", return_value=resp):
            findings = run_security_probes("http://localhost:8080", [("GET", "/actuator/env")])
        assert any(f["severity"] == "critical" for f in findings)
        assert any(f.get("cred_leak") for f in findings)

    def test_no_false_positive_for_normal_json(self):
        """Normal API response with token field in expected structure is not a finding."""
        body = b'{"user_id": 42, "expires_in": 3600}'
        resp = self._make_response(200, body)
        with patch("scripts.contract._shared.urllib.request.urlopen", return_value=resp):
            findings = run_security_probes("http://localhost:8080", [("GET", "/api/session")])
        assert findings == []

    def test_post_endpoint_gets_three_probes(self):
        responses = []
        call_count = 0

        def side_effect(*_a, **_kw):
            nonlocal call_count
            call_count += 1
            m = MagicMock()
            m.status = 200
            m.read.return_value = b"ok"
            return m

        with patch("scripts.contract._shared.urllib.request.urlopen", side_effect=side_effect):
            run_security_probes("http://localhost:8080", [("POST", "/items")])
        assert call_count == 3  # sqli, xss, long
