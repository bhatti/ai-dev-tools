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
        "state": "OPEN",
        "comments": [],
        "linked_prs": [],
    }]
    text = _format_for_analysis(issues)
    assert "#42" in text
    assert "Flaky test in CI" in text
    assert "alice" in text
    assert "bug" in text


def test_format_for_analysis_no_assignee():
    issues = [{"number": 1, "title": "T", "url": "u", "assignees": [], "labels": [],
               "body": "", "state": "OPEN", "comments": [], "linked_prs": []}]
    text = _format_for_analysis(issues)
    assert "Unassigned" in text


def test_format_for_analysis_includes_linked_prs():
    issues = [{
        "number": 10,
        "title": "Bug",
        "url": "https://github.com/org/repo/issues/10",
        "assignees": [],
        "labels": [],
        "body": "",
        "state": "OPEN",
        "comments": [],
        "linked_prs": [{"number": 99, "title": "Fix bug", "url": "https://gh/pr/99",
                        "state": "merged", "mergedAt": "2024-01-01T00:00:00Z"}],
    }]
    text = _format_for_analysis(issues)
    assert "#99" in text
    assert "Fix bug" in text


_BASE_ENV = {"GH_ORG": "org", "GH_REPO": "repo", "GH_TOKEN": "tok", "WORKSPACE_DIR": "/tmp"}

_MOCK_ISSUE = {
    "number": 42,
    "title": "Flaky test",
    "url": "https://github.com/org/repo/issues/42",
    "assignees": [],
    "labels": [],
    "body": "Fails sometimes",
    "state": "OPEN",
    "comments": [],
    "linked_prs": [],
}


@patch("scripts.gh.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.gh.analyze_issues.resolve_skill_for_analyze", return_value=None)
@patch("scripts.gh.analyze_issues.fetch_gh_issue_full", return_value=None)
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.gh.analyze_issues.run_analysis")
@patch("scripts.gh.analyze_issues.notify")
def test_main_analyze_by_numbers(mock_notify, mock_analysis, mock_fetch, mock_fetch_full,
                                  mock_skill, mock_arch):
    mock_fetch.return_value = [_MOCK_ISSUE]
    mock_analysis.return_value = "Root cause: race condition"
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "42"], env=_BASE_ENV)
    assert result.exit_code == 0
    mock_notify.assert_called_once()
    text = mock_notify.call_args[0][1]
    assert "#42" in text
    assert "race condition" in text


@patch("scripts.gh.analyze_issues._search_issues", return_value=[])
@patch("scripts.gh.analyze_issues.notify")
def test_main_no_results(mock_notify, mock_search):
    runner = CliRunner()
    result = runner.invoke(main, ["--query", "nonexistent"], env=_BASE_ENV)
    assert result.exit_code == 2
    mock_notify.assert_called_once()


@patch("scripts.gh.analyze_issues.try_git_archaeology")
@patch("scripts.gh.analyze_issues.resolve_skill_for_analyze", return_value=None)
@patch("scripts.gh.analyze_issues.emit_git_context_markers", return_value="📂 *Git context:* cloned\n")
@patch("scripts.gh.analyze_issues.fetch_gh_issue_full", return_value=None)
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.gh.analyze_issues.run_analysis")
@patch("scripts.gh.analyze_issues.notify")
def test_git_archaeology_called_when_gh_config_present(
    mock_notify, mock_analysis, mock_fetch, mock_fetch_full,
    mock_markers, mock_skill, mock_arch
):
    mock_fetch.return_value = [dict(_MOCK_ISSUE, number=123, title="Auth bug")]
    mock_arch.return_value = ("## Git History Context\n- abc fix auth", None)
    mock_analysis.return_value = "Root cause: missing null check"
    runner = CliRunner()
    env = dict(_BASE_ENV, GH_ORG="myorg", GH_REPO="myrepo")
    result = runner.invoke(main, ["--issues", "123"], env=env)
    assert result.exit_code == 0
    mock_arch.assert_called_once()
    assert "GIT_ARCHAEOLOGY::yes" in result.output


@patch("scripts.gh.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.gh.analyze_issues.resolve_skill_for_analyze")
@patch("scripts.gh.analyze_issues.fetch_gh_issue_full", return_value=None)
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.gh.analyze_issues.run_skill_analysis")
@patch("scripts.gh.analyze_issues.notify")
def test_skill_invoked_for_gh_query(mock_notify, mock_skill_analysis, mock_fetch,
                                     mock_fetch_full, mock_resolve_skill, mock_arch):
    from pathlib import Path
    mock_fetch.return_value = [dict(_MOCK_ISSUE, number=55, title="Flaky integration test")]
    mock_resolve_skill.return_value = ("ygs-analyze", Path("/fake/SKILL.md"))
    mock_skill_analysis.return_value = "Analysis: timing issue in setup"
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "55"], env=_BASE_ENV)
    assert result.exit_code == 0
    assert "SKILL_USED::ygs-analyze" in result.output


@patch("scripts.gh.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.gh.analyze_issues.resolve_skill_for_analyze")
@patch("scripts.gh.analyze_issues.fetch_gh_issue_full", return_value=None)
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.gh.analyze_issues.run_skill_analysis")
@patch("scripts.gh.analyze_issues.notify")
def test_direct_ygs_analyze_skill_lookup_gh(mock_notify, mock_skill_analysis, mock_fetch,
                                             mock_fetch_full, mock_resolve_skill, mock_arch):
    from pathlib import Path
    mock_fetch.return_value = [dict(_MOCK_ISSUE, number=99, title="crash on startup")]
    mock_resolve_skill.return_value = ("ygs-analyze", Path("/skills/ygs-analyze/SKILL.md"))
    mock_skill_analysis.return_value = "Deep GH analysis"
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "99"], env=_BASE_ENV)
    assert result.exit_code == 0
    assert "SKILL_USED::ygs-analyze" in result.output
    assert "ANALYSIS_TYPE::skill" in result.output


@patch("scripts.gh.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.gh.analyze_issues.resolve_skill_for_analyze", return_value=None)
@patch("scripts.gh.analyze_issues.fetch_gh_issue_full", return_value=None)
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.gh.analyze_issues.run_analysis")
@patch("scripts.gh.analyze_issues.notify")
def test_analysis_type_basic_when_no_skill_no_git_gh(mock_notify, mock_analysis, mock_fetch,
                                                       mock_fetch_full, mock_skill, mock_arch):
    mock_fetch.return_value = [dict(_MOCK_ISSUE, number=7, title="minor issue")]
    mock_analysis.return_value = "basic analysis"
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "7"], env=_BASE_ENV)
    assert result.exit_code == 0
    assert "ANALYSIS_TYPE::basic" in result.output
    assert "GIT_ARCHAEOLOGY::no" in result.output


@patch("scripts.gh.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.gh.analyze_issues.resolve_skill_for_analyze", return_value=None)
@patch("scripts.gh.analyze_issues.fetch_gh_issue_full", return_value=None)
@patch("scripts.common.gh_api.fetch_issues_by_numbers")
@patch("scripts.gh.analyze_issues.run_analysis")
@patch("scripts.gh.analyze_issues.notify")
def test_richer_task_context_emitted_gh(mock_notify, mock_analysis, mock_fetch,
                                         mock_fetch_full, mock_skill, mock_arch):
    mock_fetch.return_value = [dict(_MOCK_ISSUE, number=5, title="context test")]
    mock_analysis.return_value = "result"
    runner = CliRunner()
    env = dict(_BASE_ENV, AI_MODEL="claude-sonnet-test")
    result = runner.invoke(main, ["--issues", "5"], env=env)
    assert result.exit_code == 0
    assert "SELECTED_TRACKER::github" in result.output
    assert "SELECTED_MODEL::claude-sonnet-test" in result.output
    assert "ISSUE_COUNT::1" in result.output
    assert "PR_LINKS_COUNT::0" in result.output
