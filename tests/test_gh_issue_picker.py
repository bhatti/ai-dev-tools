"""Tests for scripts/gh/issue_picker.py"""

import json
import os
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from scripts.common.artifacts import read_json
from scripts.gh.issue_picker import fetch_ready_issues, main


SAMPLE_GH_RESPONSE = json.dumps([
    {
        "number": 42,
        "title": "Add user auth",
        "body": "Need OAuth2",
        "url": "https://github.com/org/repo/issues/42",
        "labels": [{"name": "ai-ready"}],
    },
    {
        "number": 43,
        "title": "Fix bug",
        "body": "It crashes",
        "url": "https://github.com/org/repo/issues/43",
        "labels": [{"name": "ai-ready"}],
    },
])


@patch("scripts.gh.issue_picker._run")
def test_fetch_ready_issues(mock_run, sample_config):
    mock_run.return_value = MagicMock(stdout=SAMPLE_GH_RESPONSE, returncode=0)
    issues = fetch_ready_issues(sample_config)
    assert len(issues) == 2
    assert issues[0]["number"] == 42


@patch("scripts.gh.issue_picker.launch_pipeline")
@patch("scripts.gh.issue_picker.gh_transition_label")
@patch("scripts.gh.issue_picker._run")
def test_main_picks_issues_and_writes_artifacts(
    mock_run, mock_transition, mock_launch, sample_config, tmp_workspace, monkeypatch
):
    monkeypatch.setenv("GH_ORG", "testorg")
    monkeypatch.setenv("GH_REPO", "testrepo")
    monkeypatch.setenv("GH_TOKEN", "ghp_test")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))

    mock_run.return_value = MagicMock(stdout=SAMPLE_GH_RESPONSE, returncode=0)
    mock_transition.return_value = None
    mock_launch.return_value = None

    runner = CliRunner()
    result = runner.invoke(main, [])
    assert result.exit_code == 0

    issue = read_json(sample_config, "42", "issue.json")
    assert issue is not None
    assert issue["number"] == 42
    assert issue["title"] == "Add user auth"


@patch("scripts.gh.issue_picker._run")
def test_main_returns_2_when_no_issues(mock_run, sample_config, tmp_workspace, monkeypatch):
    monkeypatch.setenv("GH_ORG", "testorg")
    monkeypatch.setenv("GH_REPO", "testrepo")
    monkeypatch.setenv("GH_TOKEN", "ghp_test")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))

    mock_run.return_value = MagicMock(stdout="[]", returncode=0)

    runner = CliRunner()
    result = runner.invoke(main, [])
    assert result.exit_code == 2
