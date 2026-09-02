"""Tests for scripts/gh/analyze_issues.py"""
from unittest.mock import ANY, MagicMock, patch

from click.testing import CliRunner

from scripts.common.gh_api import extract_github_numbers
from scripts.gh.analyze_issues import _format_for_analysis, main


def test_extract_numbers_bare():
    assert extract_github_numbers("123,456") == ["123", "456"]


def test_extract_numbers_with_hash():
    assert extract_github_numbers("#123, #456") == ["123", "456"]


def test_extract_numbers_from_urls():
    assert extract_github_numbers(
        "https://github.com/org/repo/issues/42, 99"
    ) == ["42", "99"]


def test_extract_numbers_empty():
    assert extract_github_numbers("") == []
    assert extract_github_numbers("not-a-number") == []


def test_format_for_analysis_includes_number():
    issues = [{
        "number": 42,
        "title": "Flaky test in CI",
        "url": "https://github.com/org/repo/issues/42",
        "assignees": [{"login": "alice"}],
        "labels": [{"name": "bug"}],
        "body": "This test fails intermittently",
    }]
    text = _format_for_analysis(issues)
    assert "#42" in text
    assert "Flaky test in CI" in text
    assert "alice" in text
    assert "bug" in text


def test_format_for_analysis_no_assignee():
    issues = [{"number": 1, "title": "T", "url": "u", "assignees": [], "labels": [], "body": ""}]
    text = _format_for_analysis(issues)
    assert "Unassigned" in text


@patch("scripts.gh.analyze_issues.find_skill", return_value=None)
@patch("scripts.gh.analyze_issues.find_skill_for_query", return_value=None)
@patch("scripts.gh.analyze_issues._try_git_archaeology", return_value=(None, None))
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.common.issue_analysis.run_claude")
@patch("scripts.gh.analyze_issues.notify")
def test_main_analyze_by_numbers(mock_notify, mock_claude, mock_fetch, mock_arch, mock_find_query, mock_find_skill):
    mock_fetch.return_value = [{
        "number": 42,
        "title": "Flaky test",
        "url": "https://github.com/org/repo/issues/42",
        "assignees": [],
        "labels": [],
        "body": "Fails sometimes",
    }]
    mock_claude.return_value = MagicMock(output="Root cause: race condition", status="DONE")
    runner = CliRunner()
    env = {
        "GH_ORG": "org", "GH_REPO": "repo", "GH_TOKEN": "tok",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--issues", "42"], env=env)
    assert result.exit_code == 0
    mock_notify.assert_called_once()
    text = mock_notify.call_args[0][1]
    assert "#42" in text
    assert "race condition" in text


@patch("scripts.gh.analyze_issues._search_issues", return_value=[])
@patch("scripts.gh.analyze_issues.notify")
def test_main_no_results(mock_notify, mock_search):
    runner = CliRunner()
    env = {
        "GH_ORG": "org", "GH_REPO": "repo", "GH_TOKEN": "tok",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--query", "nonexistent"], env=env)
    assert result.exit_code == 2
    mock_notify.assert_called_once()


@patch("scripts.gh.analyze_issues.find_skill", return_value=None)
@patch("scripts.gh.analyze_issues.find_skill_for_query", return_value=None)
@patch("scripts.gh.analyze_issues._try_git_archaeology")
@patch("scripts.common.issue_analysis.run_claude")
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.gh.analyze_issues.notify")
def test_git_archaeology_called_when_gh_config_present(
    mock_notify, mock_fetch, mock_claude, mock_arch, mock_find_query, mock_find_skill
):
    mock_fetch.return_value = [{
        "number": 123,
        "title": "Auth bug",
        "url": "https://github.com/org/repo/issues/123",
        "labels": [],
        "assignees": [],
        "state": "OPEN",
        "body": "",
    }]
    mock_arch.return_value = ("## Git History Context\n- abc fix auth", None)
    mock_claude.return_value = MagicMock(output="Root cause: missing null check", status="DONE")
    runner = CliRunner()
    env = {
        "GH_ORG": "myorg", "GH_REPO": "myrepo", "GH_TOKEN": "ghp_fake",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--issues", "123"], env=env)
    assert result.exit_code == 0
    mock_arch.assert_called_once()
    assert "GIT_ARCHAEOLOGY::yes" in result.output


@patch("scripts.gh.analyze_issues.find_skill")
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.gh.analyze_issues.run_skill_analysis")
@patch("scripts.gh.analyze_issues.notify")
def test_skill_invoked_for_gh_query(mock_notify, mock_skill_analysis, mock_fetch, mock_find_skill):
    from pathlib import Path
    mock_fetch.return_value = [{
        "number": 55,
        "title": "Flaky integration test",
        "url": "https://github.com/org/repo/issues/55",
        "labels": [],
        "assignees": [],
        "state": "OPEN",
        "body": "This test fails intermittently",
    }]
    mock_find_skill.return_value = Path("/fake/SKILL.md")
    mock_skill_analysis.return_value = "Analysis: timing issue in setup"
    runner = CliRunner()
    env = {
        "GH_ORG": "org", "GH_REPO": "repo", "GH_TOKEN": "tok",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--issues", "55"], env=env)
    assert result.exit_code == 0
    assert "SKILL_USED::ygs-analyze" in result.output


@patch("scripts.gh.analyze_issues.find_skill")
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.gh.analyze_issues.run_skill_analysis")
@patch("scripts.gh.analyze_issues.notify")
def test_direct_ygs_analyze_skill_lookup_gh(mock_notify, mock_skill_analysis, mock_fetch, mock_find_skill):
    """find_skill("ygs-analyze") called directly for gh analyze."""
    from pathlib import Path
    mock_fetch.return_value = [{
        "number": 99,
        "title": "crash on startup",
        "url": "https://github.com/org/repo/issues/99",
        "labels": [],
        "assignees": [],
        "body": "",
    }]
    mock_find_skill.return_value = Path("/skills/ygs-analyze/SKILL.md")
    mock_skill_analysis.return_value = "Deep GH analysis"
    runner = CliRunner()
    env = {
        "GH_ORG": "org", "GH_REPO": "repo", "GH_TOKEN": "tok",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--issues", "99"], env=env)
    assert result.exit_code == 0
    mock_find_skill.assert_called_with("ygs-analyze", ANY)
    assert "SKILL_USED::ygs-analyze" in result.output
    assert "ANALYSIS_TYPE::skill" in result.output


@patch("scripts.gh.analyze_issues.find_skill", return_value=None)
@patch("scripts.gh.analyze_issues.find_skill_for_query", return_value=None)
@patch("scripts.gh.analyze_issues._try_git_archaeology", return_value=(None, None))
@patch("scripts.common.issue_analysis.run_claude")
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.gh.analyze_issues.notify")
def test_analysis_type_basic_when_no_skill_no_git_gh(
    mock_notify, mock_fetch, mock_claude, mock_arch, mock_find_query, mock_find_skill
):
    """ANALYSIS_TYPE::basic emitted when no skill and no git context."""
    mock_fetch.return_value = [{
        "number": 7,
        "title": "minor issue",
        "url": "https://github.com/org/repo/issues/7",
        "labels": [],
        "assignees": [],
        "body": "",
    }]
    mock_claude.return_value = MagicMock(output="basic analysis", status="DONE")
    runner = CliRunner()
    env = {
        "GH_ORG": "org", "GH_REPO": "repo", "GH_TOKEN": "tok",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--issues", "7"], env=env)
    assert result.exit_code == 0
    assert "ANALYSIS_TYPE::basic" in result.output
    assert "GIT_ARCHAEOLOGY::no" in result.output


@patch("scripts.gh.analyze_issues.find_skill", return_value=None)
@patch("scripts.gh.analyze_issues.find_skill_for_query", return_value=None)
@patch("scripts.gh.analyze_issues._try_git_archaeology", return_value=(None, None))
@patch("scripts.common.issue_analysis.run_claude")
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.gh.analyze_issues.notify")
def test_richer_task_context_emitted_gh(
    mock_notify, mock_fetch, mock_claude, mock_arch, mock_find_query, mock_find_skill
):
    """Tracker, model, and issue count context keys are emitted for GH analyze."""
    mock_fetch.return_value = [{
        "number": 5,
        "title": "context test",
        "url": "https://github.com/org/repo/issues/5",
        "labels": [],
        "assignees": [],
        "body": "",
    }]
    mock_claude.return_value = MagicMock(output="result", status="DONE")
    runner = CliRunner()
    env = {
        "GH_ORG": "org", "GH_REPO": "repo", "GH_TOKEN": "tok",
        "WORKSPACE_DIR": "/tmp", "AI_MODEL": "claude-sonnet-test",
    }
    result = runner.invoke(main, ["--issues", "5"], env=env)
    assert result.exit_code == 0
    assert "SELECTED_TRACKER::github" in result.output
    assert "SELECTED_MODEL::claude-sonnet-test" in result.output
    assert "ISSUE_COUNT::1" in result.output
