"""Tests for scripts/review/apply_feedback.py"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from scripts.review.apply_feedback import (
    _format_review_body,
    _is_github_url,
    _is_bitbucket_url,
    _parse_github_pr,
    _parse_bitbucket_pr,
    main,
)


SAMPLE_FINDINGS = {
    "pr_url": "https://github.com/org/repo/pull/42",
    "verdict": "REQUEST_CHANGES",
    "findings": [
        {
            "severity": "HIGH",
            "confidence": "HIGH",
            "title": "SQL injection risk",
            "file": "db.py",
            "line": 42,
            "domain": "security",
            "description": "Raw SQL query",
            "fix": "Use parameterized queries",
        },
        {
            "severity": "MEDIUM",
            "confidence": "MEDIUM",
            "title": "Missing timeout",
            "file": "client.py",
            "line": 10,
            "domain": "sre",
            "description": "HTTP call without timeout",
            "fix": "Add timeout=30",
        },
    ],
    "summary": "Two issues found",
}


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def test_is_github_url():
    assert _is_github_url("https://github.com/org/repo/pull/1")
    assert not _is_github_url("https://bitbucket.org/ws/repo/pull-requests/1")


def test_is_bitbucket_url():
    assert _is_bitbucket_url("https://bitbucket.org/ws/repo/pull-requests/5")
    assert not _is_bitbucket_url("https://github.com/org/repo/pull/1")


def test_parse_github_pr():
    result = _parse_github_pr("https://github.com/myorg/myrepo/pull/99")
    assert result == ("myorg", "myrepo", "99")


def test_parse_github_pr_invalid():
    assert _parse_github_pr("https://github.com/org") is None


def test_parse_bitbucket_pr():
    result = _parse_bitbucket_pr("https://bitbucket.org/myws/myrepo/pull-requests/7")
    assert result == ("myws", "myrepo", "7")


def test_format_review_body_contains_severity():
    body = _format_review_body(SAMPLE_FINDINGS)
    assert "HIGH" in body
    assert "MEDIUM" in body
    assert "SQL injection risk" in body
    assert "db.py:42" in body


def test_format_review_body_uses_reply_when_not_confirm():
    """A non-confirm reply text is used verbatim as the review body."""
    body = _format_review_body(SAMPLE_FINDINGS, edits="My custom review comment")
    assert body == "My custom review comment"


def test_format_review_body_confirm_words_use_default():
    """'post it' should produce the default formatted body."""
    body = _format_review_body(SAMPLE_FINDINGS, edits="post it")
    assert "HIGH" in body
    assert "SQL injection risk" in body


# ---------------------------------------------------------------------------
# Approve flow
# ---------------------------------------------------------------------------

@patch("scripts.review.apply_feedback.subprocess.run")
@patch("scripts.review.apply_feedback.requests.post")
def test_approve_posts_to_github_and_exits_0(mock_post, mock_run, tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("DECISION", "approve")
    monkeypatch.setenv("GH_TOKEN", "ghp_test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "C123")

    findings_path = tmp_workspace / "findings.json"
    findings_path.write_text(json.dumps(SAMPLE_FINDINGS))

    # gh pr review succeeds
    mock_run.return_value = MagicMock(returncode=0, stderr="")
    # Slack post succeeds
    mock_post.return_value = MagicMock(ok=True, json=MagicMock(return_value={"ok": True}))

    runner = CliRunner()
    result = runner.invoke(main, ["--findings", str(findings_path)])
    assert result.exit_code == 0

    # gh pr review called with --approve
    cmd = mock_run.call_args[0][0]
    assert "gh" in cmd
    assert "--approve" in cmd


@patch("scripts.review.apply_feedback.requests.post")
def test_approve_exits_0_when_no_gh_token(mock_post, tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("DECISION", "approve")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "C123")

    findings_path = tmp_workspace / "findings.json"
    findings_path.write_text(json.dumps(SAMPLE_FINDINGS))

    mock_post.return_value = MagicMock(ok=True, json=MagicMock(return_value={"ok": True}))

    runner = CliRunner()
    result = runner.invoke(main, ["--findings", str(findings_path)])
    # Still exits 0 — GH posting is best-effort
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Request-changes flow (first pass — no reply_text → post draft + PAUSE)
# ---------------------------------------------------------------------------

@patch("scripts.review.apply_feedback.requests.post")
def test_request_changes_first_pass_posts_draft_and_exits_3(mock_post, tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("DECISION", "request-changes")
    monkeypatch.delenv("REPLY_TEXT", raising=False)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "C123")

    findings_path = tmp_workspace / "findings.json"
    findings_path.write_text(json.dumps(SAMPLE_FINDINGS))

    mock_post.return_value = MagicMock(ok=True, json=MagicMock(return_value={"ok": True}))

    runner = CliRunner()
    result = runner.invoke(main, ["--findings", str(findings_path)])
    assert result.exit_code == 3  # PAUSE_JOB — waiting for human edit/confirm

    # Slack post called with draft text
    call_args = mock_post.call_args
    payload = call_args.kwargs["json"]
    assert "Draft PR review" in payload["text"]


# ---------------------------------------------------------------------------
# Request-changes flow (second pass — reply_text confirms → post to PR + exit 0)
# ---------------------------------------------------------------------------

@patch("scripts.review.apply_feedback.requests.post")
def test_request_changes_second_pass_posts_to_pr_exits_0(mock_post, tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("DECISION", "request-changes")
    monkeypatch.setenv("REPLY_TEXT", "post it")
    monkeypatch.setenv("GH_TOKEN", "ghp_test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "C123")

    findings_path = tmp_workspace / "findings.json"
    findings_path.write_text(json.dumps(SAMPLE_FINDINGS))

    # Both GitHub API and Slack succeed
    def post_side_effect(url, **kwargs):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"id": 1, "ok": True}
        return mock_resp
    mock_post.side_effect = post_side_effect

    runner = CliRunner()
    result = runner.invoke(main, ["--findings", str(findings_path)])
    assert result.exit_code == 0

    # GitHub reviews API was called
    calls = [c.args[0] if c.args else c.kwargs.get("url", "") for c in mock_post.call_args_list]
    assert any("pulls" in str(url) and "reviews" in str(url) for url in calls)


@patch("scripts.review.apply_feedback.requests.post")
def test_request_changes_with_custom_edit_uses_verbatim(mock_post, tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("DECISION", "request-changes")
    monkeypatch.setenv("REPLY_TEXT", "Actually just fix the SQL injection, other issues are fine")
    monkeypatch.setenv("GH_TOKEN", "ghp_test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "C123")

    findings_path = tmp_workspace / "findings.json"
    findings_path.write_text(json.dumps(SAMPLE_FINDINGS))

    def post_side_effect(url, **kwargs):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"id": 1, "ok": True}
        return mock_resp
    mock_post.side_effect = post_side_effect

    runner = CliRunner()
    result = runner.invoke(main, ["--findings", str(findings_path)])
    assert result.exit_code == 0

    # The body sent to GitHub reviews API should contain the verbatim reply
    for call in mock_post.call_args_list:
        url = call.args[0] if call.args else ""
        if "reviews" in str(url):
            payload = call.kwargs["json"]
            assert "fix the SQL injection" in payload["body"]


# ---------------------------------------------------------------------------
# Verify flow
# ---------------------------------------------------------------------------

@patch("scripts.review.apply_feedback._verify_findings")
@patch("scripts.review.apply_feedback.requests.post")
def test_verify_exits_3_and_reposts_block_kit(mock_post, mock_verify, tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("DECISION", "verify")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "C123")
    monkeypatch.setenv("JOB_ID", "job-42")

    findings_path = tmp_workspace / "findings.json"
    findings_path.write_text(json.dumps(SAMPLE_FINDINGS))

    # _verify_findings returns one fewer finding
    slim_findings = dict(SAMPLE_FINDINGS)
    slim_findings["findings"] = [SAMPLE_FINDINGS["findings"][0]]  # only HIGH kept
    slim_findings["verdict"] = "REQUEST_CHANGES"
    mock_verify.return_value = slim_findings

    mock_post.return_value = MagicMock(ok=True, json=MagicMock(return_value={"ok": True, "ts": "1234.000"}))

    runner = CliRunner()
    result = runner.invoke(main, ["--findings", str(findings_path)])
    assert result.exit_code == 3  # PAUSE — human gets updated Block Kit

    # Block Kit re-post happened
    block_calls = [
        c for c in mock_post.call_args_list
        if "blocks" in (c.kwargs.get("json") or {})
    ]
    assert len(block_calls) >= 1


# ---------------------------------------------------------------------------
# Missing DECISION
# ---------------------------------------------------------------------------

def test_missing_decision_exits_1(tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.delenv("DECISION", raising=False)

    findings_path = tmp_workspace / "findings.json"
    findings_path.write_text(json.dumps(SAMPLE_FINDINGS))

    runner = CliRunner()
    result = runner.invoke(main, ["--findings", str(findings_path)])
    assert result.exit_code == 1
