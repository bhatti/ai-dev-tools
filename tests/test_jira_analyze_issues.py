"""Tests for scripts/jira/analyze_issues.py"""
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from scripts.jira.analyze_issues import _extract_keys, _extract_text_from_doc, _format_for_analysis, main


def test_extract_keys_bare_keys():
    assert _extract_keys("PROJ-123,PROJ-456") == ["PROJ-123", "PROJ-456"]


def test_extract_keys_from_urls():
    assert _extract_keys(
        "https://company.atlassian.net/browse/PROJ-43911, PROJ-43909"
    ) == ["PROJ-43911", "PROJ-43909"]


def test_extract_keys_mixed():
    keys = _extract_keys(
        "https://company.atlassian.net/browse/PROJ-1,PROJ-2,https://x.atlassian.net/browse/PROJ-3"
    )
    assert keys == ["PROJ-1", "PROJ-2", "PROJ-3"]


def test_extract_keys_empty():
    assert _extract_keys("") == []
    assert _extract_keys("not-a-key, also-not") == []


def test_extract_text_from_doc_plain():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Hello world"}
        ]}
    ]}
    assert "Hello world" in _extract_text_from_doc(doc)


def test_extract_text_from_doc_none():
    assert _extract_text_from_doc(None) == ""


def test_format_for_analysis_includes_key():
    issues = [{
        "key": "PROJ-99",
        "fields": {
            "summary": "[Flaky Test] foo.ts",
            "status": {"name": "To Do"},
            "assignee": None,
            "priority": {"name": "High"},
            "description": "Test fails intermittently",
        },
    }]
    text = _format_for_analysis(issues, "https://company.atlassian.net")
    assert "PROJ-99" in text
    assert "Flaky Test" in text
    assert "company.atlassian.net/browse/PROJ-99" in text


@patch("scripts.jira.analyze_issues._fetch_issues_by_keys")
@patch("scripts.common.issue_analysis.run_claude")
@patch("scripts.jira.analyze_issues.notify")
def test_main_analyze_by_keys(mock_notify, mock_claude, mock_fetch):
    mock_fetch.return_value = [{
        "key": "PROJ-42",
        "fields": {"summary": "[Flaky] foo", "status": {"name": "To Do"},
                   "assignee": None, "priority": {"name": "High"}, "description": ""},
    }]
    mock_claude.return_value = MagicMock(output="Root cause: race condition\nFix: add mutex", status="DONE")
    runner = CliRunner()
    env = {
        "JIRA_PROJECT": "PROJ", "JIRA_EMAIL": "u@e.com",
        "JIRA_API_TOKEN": "tok", "JIRA_BASE_URL": "https://company.atlassian.net",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--issues", "PROJ-42"], env=env)
    assert result.exit_code == 0
    mock_notify.assert_called_once()
    text = mock_notify.call_args[0][1]
    assert "PROJ-42" in text
    assert "race condition" in text


@patch("scripts.jira.query_issues._resolve_team_field_id", return_value=None)
@patch("scripts.jira.analyze_issues.search_issues", return_value=[])
@patch("scripts.jira.analyze_issues.notify")
def test_main_no_results(mock_notify, mock_search, mock_resolve):
    runner = CliRunner()
    env = {
        "JIRA_PROJECT": "PROJ", "JIRA_EMAIL": "u@e.com",
        "JIRA_API_TOKEN": "tok", "JIRA_BASE_URL": "https://company.atlassian.net",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--query", "nonexistent"], env=env)
    assert result.exit_code == 2
    mock_notify.assert_called_once()
