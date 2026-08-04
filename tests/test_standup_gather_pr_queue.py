"""Tests for gather_pr_queue — tracker selection and signals.json fast-path."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.standup.gather_pr_queue import (
    _gather_github,
    _gather_jira,
    _get_sprint_issues_with_ids,
)


# ---------------------------------------------------------------------------
# _get_sprint_issues_with_ids — signals.json fast-path
# ---------------------------------------------------------------------------

def test_get_sprint_info_reads_signals_json(tmp_path):
    """When signals.json exists with issues including numeric id, use it directly."""
    signals = {
        "sprint": {"name": "Chupacabra 379"},
        "issues": [
            {"key": "CRIBL-100", "id": "100001", "summary": "do stuff", "status": "In Progress"},
            {"key": "CRIBL-101", "id": "100002", "summary": "more stuff", "status": "In Review"},
        ],
    }
    (tmp_path / "signals.json").write_text(json.dumps(signals))

    issues, name = _get_sprint_issues_with_ids({}, workspace_dir=tmp_path)

    assert {i["key"] for i in issues} == {"CRIBL-100", "CRIBL-101"}
    assert all(i.get("id") for i in issues), "numeric id required"
    assert name == "Chupacabra 379"


def test_get_sprint_info_falls_back_when_signals_empty(tmp_path):
    """When signals.json has no issues, fall back to Jira API."""
    signals = {"sprint": {"name": "Sprint 1"}, "issues": []}
    (tmp_path / "signals.json").write_text(json.dumps(signals))

    with patch("scripts.standup.gather_pr_queue.get_active_sprints") as mock_sprints, \
         patch("scripts.standup.gather_pr_queue.get_sprint_issues") as mock_issues:
        mock_sprints.return_value = [{"id": 42, "name": "Sprint 1"}]
        mock_issues.return_value = [
            {"key": "CRIBL-200", "id": "200001", "fields": {"summary": "s", "status": {"name": "Open"}}},
            {"key": "CRIBL-201", "id": "200002", "fields": {"summary": "t", "status": {"name": "Open"}}},
        ]

        issues, name = _get_sprint_issues_with_ids({}, workspace_dir=tmp_path)

    assert {i["key"] for i in issues} == {"CRIBL-200", "CRIBL-201"}
    assert name == "Sprint 1"
    mock_sprints.assert_called_once()


def test_get_sprint_info_falls_back_when_no_signals_file(tmp_path):
    """When signals.json doesn't exist, fall back to Jira API."""
    with patch("scripts.standup.gather_pr_queue.get_active_sprints") as mock_sprints, \
         patch("scripts.standup.gather_pr_queue.get_sprint_issues") as mock_issues:
        mock_sprints.return_value = [{"id": 7, "name": "Sprint X"}]
        mock_issues.return_value = [
            {"key": "CRIBL-999", "id": "999001", "fields": {"summary": "x", "status": {"name": "In Progress"}}},
        ]

        issues, name = _get_sprint_issues_with_ids({}, workspace_dir=tmp_path)

    assert any(i["key"] == "CRIBL-999" for i in issues)
    assert name == "Sprint X"


def test_get_sprint_info_returns_empty_when_no_sprints_and_no_open_issues(tmp_path):
    """No sprints and no open issues → empty list."""
    with patch("scripts.standup.gather_pr_queue.get_active_sprints") as mock_sprints, \
         patch("scripts.standup.gather_pr_queue.search_open_issues") as mock_search:
        mock_sprints.return_value = []
        mock_search.return_value = []
        issues, name = _get_sprint_issues_with_ids({}, workspace_dir=tmp_path)

    assert issues == []
    assert name == ""


def test_get_sprint_info_falls_back_to_open_issues_when_no_sprint_keys(tmp_path):
    """When sprint has no issues, fall back to search_open_issues."""
    with patch("scripts.standup.gather_pr_queue.get_active_sprints") as mock_sprints, \
         patch("scripts.standup.gather_pr_queue.get_sprint_issues") as mock_sprint_issues, \
         patch("scripts.standup.gather_pr_queue.search_open_issues") as mock_search:
        mock_sprints.return_value = []
        mock_sprint_issues.return_value = []
        mock_search.return_value = [
            {"key": "CRIBL-500", "id": "500001", "fields": {"summary": "a", "status": {"name": "Open"}}},
            {"key": "CRIBL-501", "id": "500002", "fields": {"summary": "b", "status": {"name": "Open"}}},
        ]

        issues, name = _get_sprint_issues_with_ids({}, workspace_dir=tmp_path)

    assert any(i["key"] == "CRIBL-500" for i in issues)
    assert any(i["key"] == "CRIBL-501" for i in issues)


# ---------------------------------------------------------------------------
# Tracker selection (main logic)
# ---------------------------------------------------------------------------

def test_main_selects_jira_tracker(tmp_path, monkeypatch):
    """DEFAULT_TRACKER=jira routes to _gather_jira."""
    monkeypatch.setenv("DEFAULT_TRACKER", "jira")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example.com")
    monkeypatch.setenv("JIRA_PROJECT", "CRIBL")

    with patch("scripts.standup.gather_pr_queue._gather_jira") as mock_jira, \
         patch("scripts.standup.gather_pr_queue._gather_github") as mock_gh:
        mock_jira.return_value = {"sprint": "S1", "pr_count": 0, "prs": []}

        from scripts.standup.gather_pr_queue import main
        main()

    mock_jira.assert_called_once()
    mock_gh.assert_not_called()


def test_main_selects_github_tracker(tmp_path, monkeypatch):
    """DEFAULT_TRACKER=github routes to _gather_github."""
    monkeypatch.setenv("DEFAULT_TRACKER", "github")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("GH_ORG", "myorg")
    monkeypatch.setenv("GH_REPO", "myrepo")

    with patch("scripts.standup.gather_pr_queue._gather_jira") as mock_jira, \
         patch("scripts.standup.gather_pr_queue._gather_github") as mock_gh:
        mock_gh.return_value = {"sprint": "myorg/myrepo", "pr_count": 0, "prs": []}

        from scripts.standup.gather_pr_queue import main
        main()

    mock_gh.assert_called_once()
    mock_jira.assert_not_called()


def test_main_infers_jira_from_env(tmp_path, monkeypatch):
    """When DEFAULT_TRACKER unset, infer jira from JIRA_BASE_URL+JIRA_PROJECT."""
    monkeypatch.delenv("DEFAULT_TRACKER", raising=False)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example.com")
    monkeypatch.setenv("JIRA_PROJECT", "CRIBL")

    with patch("scripts.standup.gather_pr_queue._gather_jira") as mock_jira, \
         patch("scripts.standup.gather_pr_queue._gather_github") as mock_gh:
        mock_jira.return_value = {"sprint": "S1", "pr_count": 0, "prs": []}

        from scripts.standup.gather_pr_queue import main
        main()

    mock_jira.assert_called_once()
    mock_gh.assert_not_called()


def test_main_infers_github_from_env(tmp_path, monkeypatch):
    """When DEFAULT_TRACKER unset and no Jira config, infer github."""
    monkeypatch.delenv("DEFAULT_TRACKER", raising=False)
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    monkeypatch.delenv("JIRA_PROJECT", raising=False)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("GH_ORG", "myorg")
    monkeypatch.setenv("GH_REPO", "myrepo")

    with patch("scripts.standup.gather_pr_queue._gather_jira") as mock_jira, \
         patch("scripts.standup.gather_pr_queue._gather_github") as mock_gh:
        mock_gh.return_value = {"sprint": "myorg/myrepo", "pr_count": 0, "prs": []}

        from scripts.standup.gather_pr_queue import main
        main()

    mock_gh.assert_called_once()
    mock_jira.assert_not_called()


# ---------------------------------------------------------------------------
# _gather_jira — dev-status API integration
# ---------------------------------------------------------------------------

@patch("scripts.standup.gather_pr_queue._get_prs_for_issue")
@patch("scripts.standup.gather_pr_queue._get_sprint_issues_with_ids")
def test_gather_jira_uses_dev_status_api(mock_sprint, mock_prs, tmp_path):
    """_gather_jira calls dev-status API per issue and collects OPEN PRs."""
    mock_sprint.return_value = (
        [{"key": "CRIBL-100", "id": "100001", "summary": "do stuff", "status": "In Progress"}],
        "Sprint 1",
    )
    mock_prs.return_value = [
        {
            "id": "43677",
            "name": "CRIBL-100: Fix thing",
            "status": "OPEN",
            "url": "https://bitbucket.org/cribl/cribl/pull-requests/43677",
            "author": {"name": "Shahzad Bhatti"},
            "reviewers": [
                {"name": "Alice", "approved": True},
                {"name": "Bob", "approved": False},
            ],
        }
    ]

    config = {
        "JIRA_BASE_URL": "https://jira.example.com",
        "JIRA_PROJECT": "CRIBL",
        "JIRA_EMAIL": "user@example.com",
        "JIRA_API_TOKEN": "token",
        "BITBUCKET_WORKSPACE": "cribl",
        "BITBUCKET_REPO": "cribl",
    }
    result = _gather_jira(config, workspace_dir=tmp_path)

    assert result["pr_count"] == 1
    assert result["prs"][0]["id"] == "43677"
    assert result["prs"][0]["jira_key"] == "CRIBL-100"
    assert result["prs"][0]["approved_by"] == ["Alice"]
    assert result["prs"][0]["reviewers"] == ["Bob"]


@patch("scripts.standup.gather_pr_queue._get_prs_for_issue")
@patch("scripts.standup.gather_pr_queue._get_sprint_issues_with_ids")
def test_gather_jira_filters_non_open_prs(mock_sprint, mock_prs, tmp_path):
    """Only OPEN PRs are included; MERGED and DECLINED are excluded."""
    mock_sprint.return_value = (
        [{"key": "CRIBL-100", "id": "100001", "summary": "s", "status": "Done"}],
        "Sprint 1",
    )
    mock_prs.return_value = [
        {"id": "1", "name": "PR1", "status": "OPEN", "url": "", "author": {"name": "A"}, "reviewers": []},
        {"id": "2", "name": "PR2", "status": "MERGED", "url": "", "author": {"name": "B"}, "reviewers": []},
        {"id": "3", "name": "PR3", "status": "DECLINED", "url": "", "author": {"name": "C"}, "reviewers": []},
    ]

    config = {"JIRA_BASE_URL": "https://x", "JIRA_PROJECT": "CRIBL", "JIRA_EMAIL": "u", "JIRA_API_TOKEN": "t"}
    result = _gather_jira(config, workspace_dir=tmp_path)

    assert result["pr_count"] == 1
    assert result["prs"][0]["id"] == "1"


@patch("scripts.standup.gather_pr_queue._get_prs_for_issue")
@patch("scripts.standup.gather_pr_queue._get_sprint_issues_with_ids")
def test_gather_jira_deduplicates_prs_across_issues(mock_sprint, mock_prs, tmp_path):
    """Same PR linked to multiple issues appears only once."""
    mock_sprint.return_value = (
        [
            {"key": "CRIBL-100", "id": "100001", "summary": "a", "status": "Open"},
            {"key": "CRIBL-101", "id": "100002", "summary": "b", "status": "Open"},
        ],
        "Sprint 1",
    )
    # Both issues return the same PR id
    mock_prs.return_value = [
        {"id": "9999", "name": "Shared PR", "status": "OPEN", "url": "", "author": {"name": "Z"}, "reviewers": []},
    ]

    config = {"JIRA_BASE_URL": "https://x", "JIRA_PROJECT": "CRIBL", "JIRA_EMAIL": "u", "JIRA_API_TOKEN": "t"}
    result = _gather_jira(config, workspace_dir=tmp_path)

    assert result["pr_count"] == 1
    assert result["prs"][0]["id"] == "9999"
