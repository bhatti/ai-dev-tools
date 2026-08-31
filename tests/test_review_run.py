"""Tests for scripts/review/run.py"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from scripts.review.run import main


def _env(tmp_workspace: Path) -> dict:
    return {
        "WORKSPACE_DIR": str(tmp_workspace),
        "AI_MODEL": "claude-sonnet-4-6",
        "MAX_TURNS_IMPLEMENT": "5",
        "CLAUDE_CODE_USE_BEDROCK": "1",
    }


@patch("scripts.review.run.run_claude")
def test_run_success_writes_artifacts(mock_claude, tmp_workspace, monkeypatch):
    """Successful claude run writes review_result.json and findings.json."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)

    mock_claude.return_value = MagicMock(
        exit_code=0,
        output='Some review output\n{"status":"DONE","findings_count":2,"verdict":"REQUEST_CHANGES","summary":"Found issues"}',
        status_json={"status": "DONE", "findings_count": 2, "verdict": "REQUEST_CHANGES", "summary": "Found issues"},
        status="DONE",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["--pr-url", "https://github.com/org/repo/pull/42"])
    assert result.exit_code == 0

    review_result = json.loads((tmp_workspace / "review_result.json").read_text())
    assert review_result["status"] == "DONE"
    assert review_result["verdict"] == "REQUEST_CHANGES"

    # findings.json should be created (stub) since Claude didn't write it
    findings = json.loads((tmp_workspace / "findings.json").read_text())
    assert findings["pr_url"] == "https://github.com/org/repo/pull/42"
    assert "verdict" in findings


@patch("scripts.review.run.run_claude")
def test_run_preserves_existing_findings(mock_claude, tmp_workspace, monkeypatch):
    """If Claude wrote findings.json, run.py must not overwrite it."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)

    # Pre-populate findings.json as if Claude wrote it
    findings_data = {
        "pr_url": "https://github.com/org/repo/pull/42",
        "verdict": "APPROVE",
        "findings": [{"severity": "LOW", "title": "minor nit", "file": "main.py", "line": 5,
                      "description": "small issue", "fix": "fix it"}],
        "summary": "Looks good",
    }
    (tmp_workspace / "findings.json").write_text(json.dumps(findings_data))

    mock_claude.return_value = MagicMock(
        exit_code=0,
        output='{"status":"DONE","findings_count":1,"verdict":"APPROVE","summary":"Looks good"}',
        status_json={"status": "DONE", "findings_count": 1, "verdict": "APPROVE", "summary": "Looks good"},
        status="DONE",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["--pr-url", "https://github.com/org/repo/pull/42"])
    assert result.exit_code == 0

    # findings.json should remain as written by Claude
    preserved = json.loads((tmp_workspace / "findings.json").read_text())
    assert preserved["findings"][0]["title"] == "minor nit"


@patch("scripts.review.run.run_claude")
def test_run_exits_1_on_error(mock_claude, tmp_workspace, monkeypatch):
    """Claude RuntimeError causes exit 1 and writes error artifacts."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)

    mock_claude.side_effect = RuntimeError("claude crashed")

    runner = CliRunner()
    result = runner.invoke(main, ["--pr-url", "https://github.com/org/repo/pull/42"])
    assert result.exit_code == 1

    review_result = json.loads((tmp_workspace / "review_result.json").read_text())
    assert review_result["status"] == "ERROR"


@patch("scripts.review.run.run_claude")
def test_run_uses_custom_skill(mock_claude, tmp_workspace, monkeypatch):
    """--skill override is passed to the prompt template."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)

    mock_claude.return_value = MagicMock(
        exit_code=0,
        output='{"status":"DONE","findings_count":0,"verdict":"APPROVE","summary":"All good"}',
        status_json={"status": "DONE", "findings_count": 0, "verdict": "APPROVE", "summary": "All good"},
        status="DONE",
    )

    runner = CliRunner()
    runner.invoke(main, ["--pr-url", "https://github.com/org/repo/pull/1", "--skill", "ygs-security-review"])

    call_args = mock_claude.call_args
    prompt_used = call_args[0][0]
    assert "ygs-security-review" in prompt_used


@patch("scripts.review.run.run_claude")
def test_run_writes_prompt_to_logs(mock_claude, tmp_workspace, monkeypatch):
    """run.py saves the prompt to logs/review.prompt.txt."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)

    mock_claude.return_value = MagicMock(
        exit_code=0,
        output='{"status":"DONE","findings_count":0,"verdict":"APPROVE","summary":"ok"}',
        status_json={"status": "DONE", "findings_count": 0, "verdict": "APPROVE", "summary": "ok"},
        status="DONE",
    )

    runner = CliRunner()
    runner.invoke(main, ["--pr-url", "https://github.com/org/repo/pull/5"])

    prompt_file = tmp_workspace / "logs" / "review.prompt.txt"
    assert prompt_file.exists()
    assert "github.com/org/repo/pull/5" in prompt_file.read_text()


# ---------------------------------------------------------------------------
# self-review mode
# ---------------------------------------------------------------------------

def _setup_repo_dir(issue_dir: Path) -> Path:
    """Create a fake repo dir that _run_self_review expects to exist."""
    repo_dir = issue_dir / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    return repo_dir


@patch("scripts.review.run.run_claude")
def test_self_review_approved_exits_0(mock_claude, tmp_workspace, monkeypatch):
    """self-review with APPROVED status exits 0 and writes self_review.json."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)
    _setup_repo_dir(tmp_workspace)

    mock_claude.return_value = MagicMock(
        exit_code=0,
        output='{"status":"DONE","self_review_status":"APPROVED","findings_count":0,"notes":""}',
        status_json={"status": "DONE", "self_review_status": "APPROVED", "findings_count": 0, "notes": ""},
        status="DONE",
    )

    runner = CliRunner()
    result = runner.invoke(main, [
        "--mode", "self-review",
        "--issue-id", "42",
        "--base-branch", "main",
    ])
    assert result.exit_code == 0

    sr = json.loads((tmp_workspace / "self_review.json").read_text())
    assert sr["status"] == "APPROVED"


@patch("scripts.review.run.run_claude")
def test_self_review_blocked_exits_2(mock_claude, tmp_workspace, monkeypatch):
    """BLOCKED self-review exits 2 (pipeline must PAUSE_JOB for human review)."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)
    _setup_repo_dir(tmp_workspace)

    mock_claude.return_value = MagicMock(
        exit_code=0,
        output='{"status":"DONE","self_review_status":"BLOCKED","findings_count":2,"notes":"Critical security issue"}',
        status_json={"status": "DONE", "self_review_status": "BLOCKED", "findings_count": 2, "notes": "Critical security issue"},
        status="DONE",
    )

    runner = CliRunner()
    result = runner.invoke(main, [
        "--mode", "self-review",
        "--issue-id", "42",
        "--base-branch", "main",
    ])
    assert result.exit_code == 2


@patch("scripts.review.run.run_claude")
def test_self_review_needs_fix_exits_0(mock_claude, tmp_workspace, monkeypatch):
    """NEEDS_FIX self-review exits 0 (bot fixes inline, pipeline continues)."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)
    _setup_repo_dir(tmp_workspace)

    mock_claude.return_value = MagicMock(
        exit_code=0,
        output='{"status":"DONE","self_review_status":"NEEDS_FIX","findings_count":1,"notes":"Fixed the issue"}',
        status_json={"status": "DONE", "self_review_status": "NEEDS_FIX", "findings_count": 1, "notes": "Fixed the issue"},
        status="DONE",
    )

    runner = CliRunner()
    result = runner.invoke(main, [
        "--mode", "self-review",
        "--issue-id", "42",
        "--base-branch", "main",
    ])
    assert result.exit_code == 0


def test_self_review_missing_issue_id_exits_1(tmp_workspace, monkeypatch):
    """--mode self-review without --issue-id exits 1."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)

    runner = CliRunner()
    result = runner.invoke(main, ["--mode", "self-review"])
    assert result.exit_code == 1


def test_self_review_missing_repo_dir_exits_1(tmp_workspace, monkeypatch):
    """--mode self-review exits 1 when repo dir doesn't exist (clone not run yet)."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)
    # Deliberately NOT creating repo dir

    runner = CliRunner()
    result = runner.invoke(main, [
        "--mode", "self-review",
        "--issue-id", "99",
        "--base-branch", "main",
    ])
    assert result.exit_code == 1


@patch("scripts.review.run.run_claude")
def test_self_review_writes_prompt_log(mock_claude, tmp_workspace, monkeypatch):
    """self-review saves prompt to logs/self_review.prompt.txt with base branch."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)
    _setup_repo_dir(tmp_workspace)

    mock_claude.return_value = MagicMock(
        exit_code=0,
        output='{"status":"DONE","self_review_status":"APPROVED","findings_count":0,"notes":""}',
        status_json={"status": "DONE", "self_review_status": "APPROVED", "findings_count": 0, "notes": ""},
        status="DONE",
    )

    runner = CliRunner()
    runner.invoke(main, [
        "--mode", "self-review",
        "--issue-id", "42",
        "--base-branch", "develop",
    ])

    prompt_file = tmp_workspace / "logs" / "self_review.prompt.txt"
    assert prompt_file.exists()
    assert "develop" in prompt_file.read_text()


# ---------------------------------------------------------------------------
# Functional: primary_skill is passed to run_claude (SKILLS_INVOKED baseline)
# ---------------------------------------------------------------------------

@patch("scripts.review.run.run_claude")
def test_review_passes_primary_skill_to_run_claude(mock_claude, tmp_workspace, monkeypatch):
    """run_claude must receive primary_skill so SKILLS_INVOKED is never 'none'."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)

    mock_claude.return_value = MagicMock(
        exit_code=0,
        output='{"status":"DONE","findings_count":0,"verdict":"APPROVE","summary":"ok"}',
        status_json={"status": "DONE", "findings_count": 0, "verdict": "APPROVE", "summary": "ok"},
        status="DONE",
    )

    runner = CliRunner()
    runner.invoke(main, ["--pr-url", "https://github.com/org/repo/pull/1"])

    call_kwargs = mock_claude.call_args.kwargs
    assert "primary_skill" in call_kwargs, "run_claude must be called with primary_skill="
    assert call_kwargs["primary_skill"] is not None
    assert call_kwargs["primary_skill"] != ""


@patch("scripts.review.run.run_claude")
def test_review_deep_skill_sets_primary_skill(mock_claude, tmp_workspace, monkeypatch):
    """Explicit ygs-review-deep skill is passed as primary_skill to run_claude."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)

    mock_claude.return_value = MagicMock(
        exit_code=0,
        output='{"status":"DONE","findings_count":0,"verdict":"APPROVE","summary":"ok"}',
        status_json={"status": "DONE", "findings_count": 0, "verdict": "APPROVE", "summary": "ok"},
        status="DONE",
    )

    runner = CliRunner()
    runner.invoke(main, [
        "--pr-url", "https://github.com/org/repo/pull/1",
        "--skill", "ygs-review-deep",
    ])

    call_kwargs = mock_claude.call_args.kwargs
    assert call_kwargs.get("primary_skill") == "ygs-review-deep"


@patch("scripts.review.run.run_claude")
def test_self_review_passes_primary_skill(mock_claude, tmp_workspace, monkeypatch):
    """self-review run_claude call receives primary_skill=ygs-code-review."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)
    _setup_repo_dir(tmp_workspace)

    mock_claude.return_value = MagicMock(
        exit_code=0,
        output='{"status":"DONE","self_review_status":"APPROVED","findings_count":0,"notes":""}',
        status_json={"status": "DONE", "self_review_status": "APPROVED", "findings_count": 0, "notes": ""},
        status="DONE",
    )

    runner = CliRunner()
    runner.invoke(main, [
        "--mode", "self-review",
        "--issue-id", "42",
        "--base-branch", "main",
    ])

    call_kwargs = mock_claude.call_args.kwargs
    assert call_kwargs.get("primary_skill") == "ygs-code-review"
