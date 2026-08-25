"""Tests for scripts/gh/analyze_issues.py"""
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from scripts.gh.analyze_issues import _extract_numbers, _format_for_analysis, main


def test_extract_numbers_bare():
    assert _extract_numbers("123,456") == ["123", "456"]


def test_extract_numbers_with_hash():
    assert _extract_numbers("#123, #456") == ["123", "456"]


def test_extract_numbers_from_urls():
    assert _extract_numbers(
        "https://github.com/org/repo/issues/42, 99"
    ) == ["42", "99"]


def test_extract_numbers_empty():
    assert _extract_numbers("") == []
    assert _extract_numbers("not-a-number") == []


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


@patch("scripts.gh.analyze_issues._fetch_issues_by_numbers")
@patch("scripts.common.issue_analysis.run_claude")
@patch("scripts.gh.analyze_issues.notify")
def test_main_analyze_by_numbers(mock_notify, mock_claude, mock_fetch):
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
