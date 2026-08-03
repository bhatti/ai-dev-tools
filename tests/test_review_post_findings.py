"""Tests for scripts/review/post_findings.py"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from scripts.review.post_findings import _build_blocks, main


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


def test_build_blocks_structure():
    """Block Kit output has required block types and action buttons."""
    blocks = _build_blocks(SAMPLE_FINDINGS, job_id="job123")
    types = [b["type"] for b in blocks]
    assert "header" in types
    assert "section" in types
    assert "actions" in types

    # Find actions block
    actions = [b for b in blocks if b["type"] == "actions"][0]
    elements = actions["elements"]
    assert len(elements) == 3
    assert elements[0]["value"] == "job123:approve"
    assert elements[1]["value"] == "job123:request-changes"
    assert elements[2]["value"] == "job123:verify"


def test_build_blocks_severity_order():
    """HIGH findings appear before MEDIUM in blocks."""
    blocks = _build_blocks(SAMPLE_FINDINGS, job_id="j1")
    section_texts = [
        b["text"]["text"] for b in blocks
        if b["type"] == "section" and "text" in b
    ]
    # Find positions of HIGH and MEDIUM
    high_pos = next((i for i, t in enumerate(section_texts) if "HIGH" in t), None)
    medium_pos = next((i for i, t in enumerate(section_texts) if "MEDIUM" in t), None)
    if high_pos is not None and medium_pos is not None:
        assert high_pos < medium_pos


def test_build_blocks_empty_findings():
    """Empty findings list still produces a valid block structure."""
    data = {"pr_url": "https://github.com/x/y/pull/1", "verdict": "APPROVE",
            "findings": [], "summary": "All good"}
    blocks = _build_blocks(data, job_id="j2")
    types = [b["type"] for b in blocks]
    assert "header" in types
    assert "actions" in types


@patch("scripts.review.post_findings.requests.post")
def test_main_exits_3_on_success(mock_post, tmp_workspace, monkeypatch):
    """post_findings always exits 3 (PAUSE_JOB) after posting."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "my-channel")
    monkeypatch.setenv("SLACK_THREAD_TS", "1234567890.123")
    monkeypatch.setenv("JOB_ID", "job-42")

    # Write findings.json
    findings_path = tmp_workspace / "findings.json"
    findings_path.write_text(json.dumps(SAMPLE_FINDINGS))

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"ok": True, "ts": "9999.000"}
    mock_post.return_value = mock_resp

    runner = CliRunner()
    result = runner.invoke(main, ["--findings", str(findings_path)])
    assert result.exit_code == 3

    # Verify Slack was called
    assert mock_post.called
    call_kwargs = mock_post.call_args.kwargs
    payload = call_kwargs.get("json", {})
    assert payload["channel"] == "my-channel"
    assert payload["thread_ts"] == "1234567890.123"


@patch("scripts.review.post_findings.requests.post")
def test_main_exits_3_even_on_slack_error(mock_post, tmp_workspace, monkeypatch):
    """post_findings exits 3 even when Slack returns an error."""
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
    assert result.exit_code == 3


def test_main_exits_3_when_no_slack_token(tmp_workspace, monkeypatch):
    """post_findings exits 3 even when SLACK_BOT_TOKEN is absent."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_CHANNEL", "my-channel")

    findings_path = tmp_workspace / "findings.json"
    findings_path.write_text(json.dumps(SAMPLE_FINDINGS))

    runner = CliRunner()
    result = runner.invoke(main, ["--findings", str(findings_path)])
    assert result.exit_code == 3
