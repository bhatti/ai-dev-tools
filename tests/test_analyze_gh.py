"""Tests for scripts/gh/analyze_issues.py — analyze workflow."""
from __future__ import annotations

import re
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from scripts.gh.analyze_issues import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "GH_ORG": "myorg",
    "GH_REPO": "myrepo",
    "GH_TOKEN": "ghp_test",
    "WORKSPACE_DIR": "/tmp",
}

_FAKE_ISSUE = {
    "number": 42,
    "title": "Button click crashes on mobile",
    "url": "https://github.com/myorg/myrepo/issues/42",
    "state": "open",
    "assignees": [],
    "labels": [],
    "body": "Steps to reproduce: tap the submit button on iOS Safari.",
    "comments": [],
    "linked_prs": [],
}


# ---------------------------------------------------------------------------
# Early-exit: no issues found
# ---------------------------------------------------------------------------

@patch("scripts.gh.analyze_issues.post_report")
@patch("scripts.gh.analyze_issues.write_analysis_output")
@patch("scripts.gh.analyze_issues.resolve_github_issues", return_value=[])
@patch("scripts.gh.analyze_issues.load_config", return_value=_BASE_CONFIG)
def test_no_issues_calls_post_report_and_exits_2(
    mock_cfg, mock_resolve, mock_write, mock_post
):
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "42"])
    assert result.exit_code == 2
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs.get("title") == "No issues found"
    assert kwargs.get("task_type") == "run"


# ---------------------------------------------------------------------------
# Happy path: skill used
# ---------------------------------------------------------------------------

@patch("scripts.gh.analyze_issues.post_report")
@patch("scripts.gh.analyze_issues.write_analysis_output")
@patch("scripts.gh.analyze_issues.emit_git_context_markers", return_value="")
@patch("scripts.gh.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.gh.analyze_issues.run_skill_analysis", return_value="Skill output")
@patch("scripts.gh.analyze_issues.resolve_skill_for_analyze",
       return_value=("ygs-analyze", "/path/SKILL.md"))
@patch("scripts.gh.analyze_issues.ensure_ygs_skills")
@patch("scripts.gh.analyze_issues.fetch_gh_issue_full", return_value=_FAKE_ISSUE)
@patch("scripts.gh.analyze_issues.resolve_github_issues", return_value=[_FAKE_ISSUE])
@patch("scripts.gh.analyze_issues.load_config", return_value=_BASE_CONFIG)
def test_skill_path_skips_html_write(
    mock_cfg, mock_resolve, mock_full, mock_ygs, mock_skill_res,
    mock_skill_run, mock_arch, mock_emit, mock_write, mock_post
):
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "42"])
    assert result.exit_code == 0
    mock_write.assert_called_once()
    _, kwargs = mock_write.call_args
    assert kwargs.get("write_html") is False


@patch("scripts.gh.analyze_issues.post_report")
@patch("scripts.gh.analyze_issues.write_analysis_output")
@patch("scripts.gh.analyze_issues.emit_git_context_markers", return_value="")
@patch("scripts.gh.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.gh.analyze_issues.run_skill_analysis", return_value="Skill output")
@patch("scripts.gh.analyze_issues.resolve_skill_for_analyze",
       return_value=("ygs-analyze", "/path/SKILL.md"))
@patch("scripts.gh.analyze_issues.ensure_ygs_skills")
@patch("scripts.gh.analyze_issues.fetch_gh_issue_full", return_value=_FAKE_ISSUE)
@patch("scripts.gh.analyze_issues.resolve_github_issues", return_value=[_FAKE_ISSUE])
@patch("scripts.gh.analyze_issues.load_config", return_value=_BASE_CONFIG)
def test_post_report_called_with_safe_filename_and_run_task_type(
    mock_cfg, mock_resolve, mock_full, mock_ygs, mock_skill_res,
    mock_skill_run, mock_arch, mock_emit, mock_write, mock_post
):
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "42"])
    assert result.exit_code == 0
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs.get("task_type") == "run"
    fname = kwargs.get("filename", "")
    assert fname.endswith(".html")
    assert re.fullmatch(r"[a-zA-Z0-9_\-.]+", fname), f"unsafe filename: {fname}"


# ---------------------------------------------------------------------------
# Happy path: fallback (no skill)
# ---------------------------------------------------------------------------

@patch("scripts.gh.analyze_issues.post_report")
@patch("scripts.gh.analyze_issues.write_analysis_output")
@patch("scripts.gh.analyze_issues.emit_git_context_markers", return_value="")
@patch("scripts.gh.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.gh.analyze_issues.run_analysis", return_value="Fallback analysis")
@patch("scripts.gh.analyze_issues.resolve_skill_for_analyze", return_value=None)
@patch("scripts.gh.analyze_issues.ensure_ygs_skills")
@patch("scripts.gh.analyze_issues.fetch_gh_issue_full", return_value=_FAKE_ISSUE)
@patch("scripts.gh.analyze_issues.resolve_github_issues", return_value=[_FAKE_ISSUE])
@patch("scripts.gh.analyze_issues.load_config", return_value=_BASE_CONFIG)
def test_fallback_path_writes_html(
    mock_cfg, mock_resolve, mock_full, mock_ygs, mock_skill_res,
    mock_analysis, mock_arch, mock_emit, mock_write, mock_post
):
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "42"])
    assert result.exit_code == 0
    mock_write.assert_called_once()
    _, kwargs = mock_write.call_args
    assert kwargs.get("write_html") is True


@patch("scripts.gh.analyze_issues.post_report")
@patch("scripts.gh.analyze_issues.write_analysis_output")
@patch("scripts.gh.analyze_issues.emit_git_context_markers", return_value="")
@patch("scripts.gh.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.gh.analyze_issues.run_analysis", return_value="Fallback analysis")
@patch("scripts.gh.analyze_issues.resolve_skill_for_analyze", return_value=None)
@patch("scripts.gh.analyze_issues.ensure_ygs_skills")
@patch("scripts.gh.analyze_issues.fetch_gh_issue_full", return_value=_FAKE_ISSUE)
@patch("scripts.gh.analyze_issues.resolve_github_issues", return_value=[_FAKE_ISSUE])
@patch("scripts.gh.analyze_issues.load_config", return_value=_BASE_CONFIG)
def test_ensure_ygs_skills_called_before_skill_resolution(
    mock_cfg, mock_resolve, mock_full, mock_ygs, mock_skill_res,
    mock_analysis, mock_arch, mock_emit, mock_write, mock_post
):
    runner = CliRunner()
    runner.invoke(main, ["--issues", "42"])
    assert mock_ygs.call_count == 1
    assert mock_skill_res.call_count == 1
