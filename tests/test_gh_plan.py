"""Tests for scripts/gh/plan.py"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from scripts.common.artifacts import read_json, write_json
from scripts.gh.plan import main


@pytest.fixture
def issue_fixture(tmp_workspace, sample_issue):
    """Write a sample issue.json so plan can read it."""
    cfg = {"WORKSPACE_DIR": str(tmp_workspace)}
    write_json(cfg, "42", "issue.json", sample_issue)
    return sample_issue


def _env(tmp_workspace):
    return {
        "WORKSPACE_DIR": str(tmp_workspace),
        "GH_ORG": "testorg",
        "GH_REPO": "testrepo",
        "GH_TOKEN": "ghp_test",
        "AI_MODEL": "claude-sonnet-4-6",
        "MAX_TURNS_PLAN": "5",
    }


def test_plan_idempotent_skips_when_done(tmp_workspace, issue_fixture, monkeypatch):
    """If plan_result.json has status==DONE, plan exits 0 without calling claude."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)

    cfg = {"WORKSPACE_DIR": str(tmp_workspace)}
    write_json(cfg, "42", "plan_result.json", {"status": "DONE", "plan_file": "plan.md"})

    with patch("scripts.gh.plan.run_claude") as mock_claude:
        runner = CliRunner()
        result = runner.invoke(main, ["--issue-id", "42"])
        assert result.exit_code == 0
        mock_claude.assert_not_called()


@patch("scripts.gh.plan.run_claude")
def test_plan_writes_artifacts_on_success(mock_claude, tmp_workspace, issue_fixture, monkeypatch):
    """Successful claude run writes plan.md and plan_result.json."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)

    plan_dir = tmp_workspace / "42"
    plan_dir.mkdir(parents=True, exist_ok=True)
    # Simulate claude writing a plan file
    (plan_dir / "plan.md").write_text("# Plan\n- Step 1\n- Step 2\n")

    mock_claude.return_value = MagicMock(
        exit_code=0,
        output='# Plan\n- Step 1\n{"status":"DONE","summary":"Implemented OAuth"}',
        status_json={"status": "DONE", "summary": "Implemented OAuth"},
        status="DONE",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["--issue-id", "42"])
    assert result.exit_code == 0

    cfg = {"WORKSPACE_DIR": str(tmp_workspace)}
    plan_result = read_json(cfg, "42", "plan_result.json")
    assert plan_result is not None
    assert plan_result["status"] == "DONE"


@patch("scripts.gh.plan.run_claude")
def test_plan_exits_2_when_blocked(mock_claude, tmp_workspace, issue_fixture, monkeypatch):
    """BLOCKED status causes exit code 2."""
    for k, v in _env(tmp_workspace).items():
        monkeypatch.setenv(k, v)

    mock_claude.return_value = MagicMock(
        exit_code=0,
        output='{"status":"BLOCKED","reason":"Need more info"}',
        status_json={"status": "BLOCKED", "reason": "Need more info"},
        status="BLOCKED",
    )

    runner = CliRunner()
    result = runner.invoke(main, ["--issue-id", "42"])
    assert result.exit_code == 2
