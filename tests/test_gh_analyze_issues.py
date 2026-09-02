"""Tests for scripts/gh/analyze_issues.py"""
from unittest.mock import MagicMock, patch

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


@patch("scripts.gh.analyze_issues.find_skill_for_query", return_value=None)
@patch("scripts.gh.analyze_issues._try_git_archaeology", return_value=None)
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.common.issue_analysis.run_claude")
@patch("scripts.gh.analyze_issues.notify")
def test_main_analyze_by_numbers(mock_notify, mock_claude, mock_fetch, mock_arch, mock_find):
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


@patch("scripts.gh.analyze_issues.find_skill_for_query", return_value=None)
@patch("scripts.gh.analyze_issues._try_git_archaeology")
@patch("scripts.common.issue_analysis.run_claude")
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.gh.analyze_issues.notify")
def test_git_archaeology_called_when_gh_config_present(
    mock_notify, mock_fetch, mock_claude, mock_arch, mock_find
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
    mock_arch.return_value = "## Git History Context\n- abc fix auth"
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


@patch("scripts.gh.analyze_issues.find_skill_for_query")
@patch("scripts.gh.analyze_issues._run_skill_analysis")
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.gh.analyze_issues.notify")
def test_skill_invoked_for_gh_query(mock_notify, mock_fetch, mock_skill_analysis, mock_find_skill):
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
    mock_find_skill.return_value = ("ygs-analyze", Path("/fake/SKILL.md"))
    mock_skill_analysis.return_value = "Analysis: timing issue in setup"
    runner = CliRunner()
    env = {
        "GH_ORG": "org", "GH_REPO": "repo", "GH_TOKEN": "tok",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--issues", "55"], env=env)
    assert result.exit_code == 0
    assert "SKILL_USED::ygs-analyze" in result.output
