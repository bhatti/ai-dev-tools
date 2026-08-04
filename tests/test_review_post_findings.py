"""Tests for scripts/review/post_findings.py"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from scripts.review.post_findings import _build_text, main


SAMPLE_FINDINGS = {
    "pr_url": "https://github.com/org/repo/pull/42",
    "verdict": "REQUEST_CHANGES",
    "findings": [
        {"severity": "HIGH", "confidence": "HIGH", "title": "SQL injection risk",
         "file": "db.py", "line": 42, "domain": "security",
         "description": "Raw SQL query", "fix": "Use parameterized queries"},
        {"severity": "MEDIUM", "confidence": "MEDIUM", "title": "Missing timeout",
         "file": "client.py", "line": 10, "domain": "sre",
         "description": "HTTP call without timeout", "fix": "Add timeout=30"},
    ],
    "summary": "Two issues found",
}


def test_build_text_contains_verdict():
    text = _build_text(SAMPLE_FINDINGS)
    assert "REQUEST_CHANGES" in text


def test_build_text_severity_order():
    """HIGH findings appear before MEDIUM in the text."""
    text = _build_text(SAMPLE_FINDINGS)
    high_pos = text.find("HIGH")
    medium_pos = text.find("MEDIUM")
    assert high_pos != -1
    assert medium_pos != -1
    assert high_pos < medium_pos


def test_build_text_empty_findings():
    data = {"pr_url": "https://github.com/x/y/pull/1", "verdict": "APPROVE",
            "findings": [], "summary": "All good"}
    text = _build_text(data)
    assert "APPROVE" in text
    assert "All good" in text


def test_build_text_includes_pr_url():
    text = _build_text(SAMPLE_FINDINGS)
    assert "https://github.com/org/repo/pull/42" in text


@patch("scripts.review.post_findings.requests.post")
def test_main_exits_0_on_success(mock_post, tmp_workspace, monkeypatch):
    """post_findings exits 0 after successfully posting to Slack."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "my-channel")
    monkeypatch.setenv("SLACK_THREAD_TS", "1234567890.123")

    findings_path = tmp_workspace / "findings.json"
    findings_path.write_text(json.dumps(SAMPLE_FINDINGS))

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"ok": True, "ts": "9999.000"}
    mock_post.return_value = mock_resp

    runner = CliRunner()
    result = runner.invoke(main, ["--findings", str(findings_path)])
    assert result.exit_code == 0

    call_kwargs = mock_post.call_args.kwargs
    payload = call_kwargs.get("json", {})
    assert payload["channel"] == "my-channel"
    assert payload["thread_ts"] == "1234567890.123"

    result_file = tmp_workspace / "post_result.json"
    assert result_file.exists()
    data = json.loads(result_file.read_text())
    assert data["status"] == "POSTED"


@patch("scripts.review.post_findings.requests.post")
def test_main_exits_0_on_slack_error(mock_post, tmp_workspace, monkeypatch):
    """post_findings exits 0 even when Slack returns an API error (non-fatal)."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "my-channel")

    findings_path = tmp_workspace / "findings.json"
    findings_path.write_text(json.dumps(SAMPLE_FINDINGS))

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"ok": False, "error": "channel_not_found"}
    mock_post.return_value = mock_resp

    runner = CliRunner()
    result = runner.invoke(main, ["--findings", str(findings_path)])
    assert result.exit_code == 0

    result_file = tmp_workspace / "post_result.json"
    data = json.loads(result_file.read_text())
    assert data["status"] == "FAILED"


def test_main_exits_0_when_no_slack_token(tmp_workspace, monkeypatch):
    """post_findings exits 0 (skipped) when SLACK_BOT_TOKEN is absent."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_CHANNEL", "my-channel")

    findings_path = tmp_workspace / "findings.json"
    findings_path.write_text(json.dumps(SAMPLE_FINDINGS))

    runner = CliRunner()
    result = runner.invoke(main, ["--findings", str(findings_path)])
    assert result.exit_code == 0

    result_file = tmp_workspace / "post_result.json"
    data = json.loads(result_file.read_text())
    assert data["status"] == "SKIPPED"


def test_main_handles_missing_findings(tmp_workspace, monkeypatch):
    """post_findings handles missing findings.json gracefully."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "my-channel")

    with patch("scripts.review.post_findings.requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"ok": True, "ts": "1.0"}
        mock_post.return_value = mock_resp

        runner = CliRunner()
        result = runner.invoke(main, ["--findings", str(tmp_workspace / "nonexistent.json")])

    assert result.exit_code == 0
