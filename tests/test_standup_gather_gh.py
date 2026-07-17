"""Tests for scripts/standup/gather_gh.py"""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from scripts.standup.gather_gh import (
    _normalise_issue,
    get_open_issues,
    get_open_prs,
    main,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def gh_config(tmp_workspace):
    return {
        "WORKSPACE_DIR": str(tmp_workspace),
        "GH_ORG": "testorg",
        "GH_REPO": "testrepo",
        "GH_TOKEN": "ghp_test",
        "STANDUP_LOOKBACK_HOURS": "26",
        "STANDUP_STALE_DAYS": "2",
    }


_DEFAULT_ASSIGNEES = ["alice"]


def _make_gh_issue(number=1, assignees=_DEFAULT_ASSIGNEES, labels=None, updated_iso=None):
    updated = updated_iso or datetime.now(timezone.utc).isoformat()
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": "description",
        "url": f"https://github.com/org/repo/issues/{number}",
        "labels": [{"name": l} for l in (labels or [])],
        "assignees": [{"login": a} for a in assignees],
        "updatedAt": updated,
        "createdAt": updated,
        "comments": [],
    }


# ---------------------------------------------------------------------------
# _normalise_issue
# ---------------------------------------------------------------------------

def test_normalise_issue_fresh():
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    raw = _make_gh_issue(number=42, assignees=["alice"])
    result = _normalise_issue(raw, stale_cutoff)
    assert result["key"] == "#42"
    assert result["assignee"] == "alice"
    assert result["is_stale"] is False
    assert result["is_blocked"] is False


def test_normalise_issue_stale():
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    raw = _make_gh_issue(updated_iso=old)
    result = _normalise_issue(raw, stale_cutoff)
    assert result["is_stale"] is True
    assert result["stale_days"] >= 5


def test_normalise_issue_blocked_by_label():
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    raw = _make_gh_issue(labels=["blocked", "bug"])
    result = _normalise_issue(raw, stale_cutoff)
    assert result["is_blocked"] is True


def test_normalise_issue_unassigned():
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    raw = _make_gh_issue(assignees=[])
    result = _normalise_issue(raw, stale_cutoff)
    assert result["assignee"] == "unassigned"


# ---------------------------------------------------------------------------
# get_open_issues
# ---------------------------------------------------------------------------

@patch("scripts.standup.gather_gh._run")
def test_get_open_issues(mock_run, gh_config):
    issues = [_make_gh_issue(1), _make_gh_issue(2)]
    mock_run.return_value = MagicMock(stdout=json.dumps(issues), returncode=0)
    result = get_open_issues(gh_config)
    assert len(result) == 2


@patch("scripts.standup.gather_gh._run")
def test_get_open_issues_filters_by_team(mock_run, gh_config):
    issues = [
        _make_gh_issue(1, assignees=["alice"]),
        _make_gh_issue(2, assignees=["bob"]),
        _make_gh_issue(3, assignees=["charlie"]),
    ]
    mock_run.return_value = MagicMock(stdout=json.dumps(issues), returncode=0)
    config = {**gh_config, "STANDUP_TEAM_MEMBERS": "alice,charlie"}
    result = get_open_issues(config)
    # gh CLI filters by first member; post-filter keeps alice + charlie
    logins = [r["assignees"][0]["login"] for r in result]
    assert "bob" not in logins


@patch("scripts.standup.gather_gh._run")
def test_get_open_issues_empty(mock_run, gh_config):
    mock_run.return_value = MagicMock(stdout="", returncode=0)
    result = get_open_issues(gh_config)
    assert result == []


@patch("scripts.standup.gather_gh._run")
def test_get_open_issues_bad_json(mock_run, gh_config):
    mock_run.return_value = MagicMock(stdout="not json", returncode=0)
    result = get_open_issues(gh_config)
    assert result == []


# ---------------------------------------------------------------------------
# get_open_prs
# ---------------------------------------------------------------------------

@patch("scripts.standup.gather_gh._run")
def test_get_open_prs(mock_run, gh_config):
    prs_data = [
        {
            "number": 10,
            "title": "Add feature",
            "author": {"login": "alice"},
            "createdAt": "2026-07-14T10:00:00Z",
            "reviews": [],
            "reviewRequests": [{"login": "bob"}],
            "url": "https://github.com/org/repo/pull/10",
            "headRefName": "feature/add-auth",
        }
    ]
    mock_run.return_value = MagicMock(stdout=json.dumps(prs_data), returncode=0)
    prs = get_open_prs(gh_config)
    assert len(prs) == 1
    assert prs[0]["author"] == "alice"
    assert "bob" in prs[0]["reviewers"]
    assert prs[0]["age_hours"] > 0
    assert prs[0]["has_approval"] is False


@patch("scripts.standup.gather_gh._run")
def test_get_open_prs_with_approval(mock_run, gh_config):
    prs_data = [
        {
            "number": 11,
            "title": "Fix bug",
            "author": {"login": "bob"},
            "createdAt": "2026-07-16T08:00:00Z",
            "reviews": [{"state": "APPROVED"}],
            "reviewRequests": [],
            "url": "https://github.com/org/repo/pull/11",
            "headRefName": "fix/bug",
        }
    ]
    mock_run.return_value = MagicMock(stdout=json.dumps(prs_data), returncode=0)
    prs = get_open_prs(gh_config)
    assert prs[0]["has_approval"] is True


@patch("scripts.standup.gather_gh._run")
def test_get_open_prs_empty(mock_run, gh_config):
    mock_run.return_value = MagicMock(stdout="[]", returncode=0)
    prs = get_open_prs(gh_config)
    assert prs == []


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

@patch("scripts.standup.gather_gh.get_standup_messages", return_value=[])
@patch("scripts.standup.gather_gh.get_open_prs")
@patch("scripts.standup.gather_gh.get_open_issues")
def test_main_writes_signals(mock_issues, mock_prs, mock_slack, tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("GH_ORG", "testorg")
    monkeypatch.setenv("GH_REPO", "testrepo")
    monkeypatch.setenv("GH_TOKEN", "ghp_test")

    mock_issues.return_value = [_make_gh_issue(1), _make_gh_issue(2)]
    mock_prs.return_value = []

    from scripts.standup.gather_gh import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0

    signals = json.loads((tmp_workspace / "signals.json").read_text())
    assert signals["tracker"] == "github"
    assert len(signals["issues"]) == 2

    result = json.loads((tmp_workspace / "gather_result.json").read_text())
    assert result["status"] == "DONE"
    assert result["tracker"] == "github"
    assert result["issue_count"] == 2
