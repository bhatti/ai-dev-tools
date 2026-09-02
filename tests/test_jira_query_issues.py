"""Tests for scripts/jira/query_issues.py"""
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from scripts.jira.query_issues import _build_jql, _format_issue, main


BASE_CONFIG = {
    "JIRA_PROJECT": "PROJ",
    "JIRA_EMAIL": "user@example.com",
    "JIRA_API_TOKEN": "token",
    "JIRA_BASE_URL": "https://company.atlassian.net",
}


@patch("scripts.jira.query_issues._resolve_team_field_id", return_value="customfield_10248")
def test_build_jql_with_space(mock_resolve):
    cfg = {**BASE_CONFIG, "JIRA_SPACE": "Distributed Management"}
    jql = _build_jql(cfg, "flaky")
    assert 'project = "PROJ"' in jql
    assert 'customfield_10248 = "Distributed Management"' in jql
    assert 'summary ~ "flaky"' in jql
    assert 'status not in' in jql
    assert "Done" in jql


@patch("scripts.jira.query_issues._resolve_team_field_id", return_value="customfield_10248")
def test_build_jql_space_falls_back_to_bitbucket_workspace(mock_resolve):
    cfg = {**BASE_CONFIG, "BITBUCKET_WORKSPACE": "myorg"}
    jql = _build_jql(cfg, "flaky")
    assert 'customfield_10248 = "myorg"' in jql


def test_build_jql_no_space():
    jql = _build_jql(BASE_CONFIG, "flaky")
    # No space → no team filter
    assert 'customfield_10248' not in jql
    assert 'summary ~ "flaky"' in jql


def test_build_jql_with_issue_type():
    jql = _build_jql(BASE_CONFIG, "flaky", issue_type="Bug")
    assert 'issuetype = "Bug"' in jql


def test_build_jql_empty_query():
    jql = _build_jql(BASE_CONFIG, "")
    assert "summary ~" not in jql
    assert "ORDER BY" in jql


@patch("scripts.jira.query_issues._resolve_team_field_id", return_value=None)
def test_build_jql_team_field_not_found_skips_filter(mock_resolve):
    cfg = {**BASE_CONFIG, "JIRA_SPACE": "My Team"}
    jql = _build_jql(cfg, "flaky")
    assert 'customfield_10248' not in jql
    assert 'summary ~ "flaky"' in jql


def test_build_jql_team_field_disabled():
    cfg = {**BASE_CONFIG, "JIRA_SPACE": "My Team", "JIRA_TEAM_FIELD": ""}
    jql = _build_jql(cfg, "flaky")
    # JIRA_TEAM_FIELD="" disables the filter entirely
    assert 'customfield' not in jql


def test_format_issue_full():
    issue = {
        "key": "PROJ-123",
        "fields": {
            "summary": "[Flaky Test] file='foo.ts' (1 test)",
            "status": {"name": "To Do"},
            "assignee": {"displayName": "Alice"},
            "priority": {"name": "High"},
        },
    }
    line = _format_issue(issue, "https://company.atlassian.net")
    assert "PROJ-123" in line
    assert "Flaky Test" in line
    assert "Alice" in line
    assert "To Do" in line
    assert "High" in line
    assert "https://company.atlassian.net/browse/PROJ-123" in line


def test_format_issue_unassigned():
    issue = {
        "key": "PROJ-99",
        "fields": {"summary": "Bug", "status": {"name": "Open"}, "assignee": None, "priority": None},
    }
    line = _format_issue(issue, "https://company.atlassian.net")
    assert "Unassigned" in line


@patch("scripts.common.jira_api.search_issues")
@patch("scripts.jira.query_issues.notify")
def test_main_posts_results(mock_notify, mock_search):
    mock_search.return_value = [
        {
            "key": "PROJ-42",
            "fields": {
                "summary": "[Flaky Test] foo.ts",
                "status": {"name": "To Do"},
                "assignee": None,
                "priority": {"name": "High"},
            },
        }
    ]
    runner = CliRunner()
    env = {
        "JIRA_PROJECT": "PROJ",
        "JIRA_EMAIL": "u@e.com",
        "JIRA_API_TOKEN": "tok",
        "JIRA_BASE_URL": "https://company.atlassian.net",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--query", "flaky"], env=env)
    assert result.exit_code == 0
    mock_notify.assert_called_once()
    text = mock_notify.call_args[0][1]
    assert "PROJ-42" in text
    assert "Flaky Test" in text


@patch("scripts.common.jira_api.search_issues", return_value=[])
@patch("scripts.jira.query_issues.notify")
def test_main_no_results(mock_notify, mock_search):
    runner = CliRunner()
    env = {
        "JIRA_PROJECT": "PROJ",
        "JIRA_EMAIL": "u@e.com",
        "JIRA_API_TOKEN": "tok",
        "JIRA_BASE_URL": "https://company.atlassian.net",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--query", "nonexistent"], env=env)
    assert result.exit_code == 2
    mock_notify.assert_called_once()
    assert "No open" in mock_notify.call_args[0][1]
