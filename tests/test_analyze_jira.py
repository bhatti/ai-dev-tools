"""Tests for scripts/jira/analyze_issues.py — analyze workflow."""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
from click.testing import CliRunner

from scripts.jira.analyze_issues import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CONFIG = {
    "JIRA_EMAIL": "test@example.com",
    "JIRA_API_TOKEN": "token",
    "JIRA_BASE_URL": "https://company.atlassian.net",
    "JIRA_PROJECT": "PROJ",
    "WORKSPACE_DIR": "/tmp",
}

_FAKE_ISSUE = {
    "key": "PROJ-1",
    "id": "1001",
    "fields": {
        "summary": "Login fails on Safari",
        "status": {"name": "Open"},
        "priority": {"name": "High"},
        "assignee": {"displayName": "Alice"},
        "description": None,
        "issuelinks": [],
        "attachment": [],
    },
}


# ---------------------------------------------------------------------------
# Early-exit: no issues found
# ---------------------------------------------------------------------------

@patch("scripts.jira.analyze_issues.post_report")
@patch("scripts.jira.analyze_issues.write_analysis_output")
@patch("scripts.jira.analyze_issues.resolve_jira_issues", return_value=[])
@patch("scripts.jira.analyze_issues.load_config", return_value=_BASE_CONFIG)
def test_no_issues_calls_post_report_and_exits_2(
    mock_cfg, mock_resolve, mock_write, mock_post
):
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "PROJ-1"])
    assert result.exit_code == 2
    mock_post.assert_called_once()
    call_kwargs = mock_post.call_args
    assert call_kwargs.kwargs.get("title") == "No issues found"
    assert call_kwargs.kwargs.get("task_type") == "query"


# ---------------------------------------------------------------------------
# Happy path: skill used
# ---------------------------------------------------------------------------

@patch("scripts.jira.analyze_issues.post_report")
@patch("scripts.jira.analyze_issues.write_analysis_output")
@patch("scripts.jira.analyze_issues.emit_git_context_markers", return_value="")
@patch("scripts.jira.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.jira.analyze_issues.run_skill_analysis", return_value="Skill output")
@patch("scripts.jira.analyze_issues.resolve_skill_for_analyze",
       return_value=("ygs-analyze", "/path/SKILL.md"))
@patch("scripts.jira.analyze_issues.ensure_ygs_skills")
@patch("scripts.jira.analyze_issues.fetch_jira_issue_full", return_value=_FAKE_ISSUE)
@patch("scripts.jira.analyze_issues.get_jira_linked_prs", return_value=[])
@patch("scripts.jira.analyze_issues.fetch_jira_attachment_text", return_value="")
@patch("scripts.jira.analyze_issues.resolve_jira_issues", return_value=[_FAKE_ISSUE])
@patch("scripts.jira.analyze_issues.load_config", return_value=_BASE_CONFIG)
def test_skill_path_skips_html_write(
    mock_cfg, mock_resolve, mock_attach, mock_prs, mock_full,
    mock_ygs, mock_skill_res, mock_skill_run, mock_arch, mock_emit,
    mock_write, mock_post
):
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "PROJ-1"])
    assert result.exit_code == 0
    # write_html=False when skill_result is set
    mock_write.assert_called_once()
    _, kwargs = mock_write.call_args
    assert kwargs.get("write_html") is False


@patch("scripts.jira.analyze_issues.post_report")
@patch("scripts.jira.analyze_issues.write_analysis_output")
@patch("scripts.jira.analyze_issues.emit_git_context_markers", return_value="")
@patch("scripts.jira.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.jira.analyze_issues.run_skill_analysis", return_value="Skill output")
@patch("scripts.jira.analyze_issues.resolve_skill_for_analyze",
       return_value=("ygs-analyze", "/path/SKILL.md"))
@patch("scripts.jira.analyze_issues.ensure_ygs_skills")
@patch("scripts.jira.analyze_issues.fetch_jira_issue_full", return_value=_FAKE_ISSUE)
@patch("scripts.jira.analyze_issues.get_jira_linked_prs", return_value=[])
@patch("scripts.jira.analyze_issues.fetch_jira_attachment_text", return_value="")
@patch("scripts.jira.analyze_issues.resolve_jira_issues", return_value=[_FAKE_ISSUE])
@patch("scripts.jira.analyze_issues.load_config", return_value=_BASE_CONFIG)
def test_post_report_called_with_mrkdwn_and_task_type(
    mock_cfg, mock_resolve, mock_attach, mock_prs, mock_full,
    mock_ygs, mock_skill_res, mock_skill_run, mock_arch, mock_emit,
    mock_write, mock_post
):
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "PROJ-1"])
    assert result.exit_code == 0
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs.get("task_type") == "query"
    # filename must be filesystem-safe (no spaces/special chars)
    fname = kwargs.get("filename", "")
    assert fname.endswith(".html")
    import re
    assert re.fullmatch(r"[a-zA-Z0-9_\-.]+", fname), f"unsafe filename: {fname}"


# ---------------------------------------------------------------------------
# Happy path: fallback (no skill)
# ---------------------------------------------------------------------------

@patch("scripts.jira.analyze_issues.post_report")
@patch("scripts.jira.analyze_issues.write_analysis_output")
@patch("scripts.jira.analyze_issues.emit_git_context_markers", return_value="")
@patch("scripts.jira.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.jira.analyze_issues.run_analysis", return_value="Fallback output")
@patch("scripts.jira.analyze_issues.resolve_skill_for_analyze", return_value=None)
@patch("scripts.jira.analyze_issues.ensure_ygs_skills")
@patch("scripts.jira.analyze_issues.fetch_jira_issue_full", return_value=_FAKE_ISSUE)
@patch("scripts.jira.analyze_issues.get_jira_linked_prs", return_value=[])
@patch("scripts.jira.analyze_issues.fetch_jira_attachment_text", return_value="")
@patch("scripts.jira.analyze_issues.resolve_jira_issues", return_value=[_FAKE_ISSUE])
@patch("scripts.jira.analyze_issues.load_config", return_value=_BASE_CONFIG)
def test_fallback_path_writes_html(
    mock_cfg, mock_resolve, mock_attach, mock_prs, mock_full,
    mock_ygs, mock_skill_res, mock_analysis, mock_arch, mock_emit,
    mock_write, mock_post
):
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "PROJ-1"])
    assert result.exit_code == 0
    mock_write.assert_called_once()
    _, kwargs = mock_write.call_args
    # write_html=True when no skill_result
    assert kwargs.get("write_html") is True


@patch("scripts.jira.analyze_issues.post_report")
@patch("scripts.jira.analyze_issues.write_analysis_output")
@patch("scripts.jira.analyze_issues.emit_git_context_markers", return_value="")
@patch("scripts.jira.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.jira.analyze_issues.run_analysis", return_value="Fallback output")
@patch("scripts.jira.analyze_issues.resolve_skill_for_analyze", return_value=None)
@patch("scripts.jira.analyze_issues.ensure_ygs_skills")
@patch("scripts.jira.analyze_issues.fetch_jira_issue_full", return_value=_FAKE_ISSUE)
@patch("scripts.jira.analyze_issues.get_jira_linked_prs", return_value=[])
@patch("scripts.jira.analyze_issues.fetch_jira_attachment_text", return_value="")
@patch("scripts.jira.analyze_issues.resolve_jira_issues", return_value=[_FAKE_ISSUE])
@patch("scripts.jira.analyze_issues.load_config", return_value=_BASE_CONFIG)
def test_ensure_ygs_skills_called_before_skill_resolution(
    mock_cfg, mock_resolve, mock_attach, mock_prs, mock_full,
    mock_ygs, mock_skill_res, mock_analysis, mock_arch, mock_emit,
    mock_write, mock_post
):
    runner = CliRunner()
    runner.invoke(main, ["--issues", "PROJ-1"])
    # ensure_ygs_skills must have been called before resolve_skill_for_analyze
    assert mock_ygs.call_count == 1
    assert mock_skill_res.call_count == 1
    mock_ygs.assert_called_once()
