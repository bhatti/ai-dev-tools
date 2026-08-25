"""Tests for scripts/standup/gather_jira.py"""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from scripts.standup.gather_jira import (
    _adf_text,
    _extract_embedded_comments,
    _normalise_issue,
    get_active_sprint,
    get_active_sprints,
    get_current_jira_user,
    get_sprint_issues,
    search_open_issues,
    search_my_open_issues,
    main,
)
from scripts.standup.bb_helpers import get_open_prs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def jira_config(tmp_workspace):
    return {
        "WORKSPACE_DIR": str(tmp_workspace),
        "JIRA_BASE_URL": "https://test.atlassian.net",
        "JIRA_EMAIL": "test@example.com",
        "JIRA_API_TOKEN": "token123",
        "JIRA_PROJECT": "PROJ",
        "STANDUP_LOOKBACK_HOURS": "26",
        "STANDUP_STALE_DAYS": "2",
    }


def _make_raw_issue(key="PROJ-1", assignee_name="Alice", updated_iso=None, labels=None, status="In Progress"):
    updated = updated_iso or datetime.now(timezone.utc).isoformat()
    return {
        "key": key,
        "fields": {
            "summary": f"Summary of {key}",
            "status": {"name": status},
            "assignee": {"displayName": assignee_name, "accountId": "acc-1"},
            "updated": updated,
            "labels": labels or [],
            "priority": {"name": "Medium"},
            "comment": {"comments": []},
        },
    }


# ---------------------------------------------------------------------------
# _adf_text
# ---------------------------------------------------------------------------

def test_adf_text_plain():
    node = {"type": "text", "text": "Hello world"}
    assert _adf_text(node) == "Hello world"


def test_adf_text_nested():
    node = {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "Part one"},
            {"type": "text", "text": "part two"},
        ],
    }
    assert "Part one" in _adf_text(node)
    assert "part two" in _adf_text(node)


# ---------------------------------------------------------------------------
# _extract_embedded_comments
# ---------------------------------------------------------------------------

def test_extract_embedded_comments_recent():
    now_iso = datetime.now(timezone.utc).isoformat()
    fields = {
        "comment": {
            "comments": [
                {
                    "created": now_iso,
                    "author": {"displayName": "Bob"},
                    "body": {"type": "text", "text": "Fixed it"},
                }
            ]
        }
    }
    result = _extract_embedded_comments(fields, lookback_hours=26)
    assert len(result) == 1
    assert result[0]["author"] == "Bob"
    assert "Fixed it" in result[0]["text"]


def test_extract_embedded_comments_filters_old():
    old_iso = (datetime.now(timezone.utc) - timedelta(hours=50)).isoformat()
    fields = {
        "comment": {
            "comments": [
                {
                    "created": old_iso,
                    "author": {"displayName": "Bob"},
                    "body": "old comment",
                }
            ]
        }
    }
    result = _extract_embedded_comments(fields, lookback_hours=26)
    assert len(result) == 0


def test_extract_embedded_comments_empty():
    assert _extract_embedded_comments({}, lookback_hours=26) == []
    assert _extract_embedded_comments({"comment": {}}, lookback_hours=26) == []


# ---------------------------------------------------------------------------
# _normalise_issue
# ---------------------------------------------------------------------------

def test_normalise_issue_fresh(jira_config):
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    raw = _make_raw_issue(updated_iso=datetime.now(timezone.utc).isoformat())
    result = _normalise_issue(raw, stale_cutoff, jira_config, 26)
    assert result["key"] == "PROJ-1"
    assert result["assignee"] == "Alice"
    assert result["is_stale"] is False
    assert result["is_blocked"] is False


def test_normalise_issue_stale(jira_config):
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    raw = _make_raw_issue(updated_iso=old)
    result = _normalise_issue(raw, stale_cutoff, jira_config, 26)
    assert result["is_stale"] is True
    assert result["stale_days"] >= 5


def test_normalise_issue_blocked_by_label(jira_config):
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    raw = _make_raw_issue(labels=["blocked"])
    result = _normalise_issue(raw, stale_cutoff, jira_config, 26)
    assert result["is_blocked"] is True


def test_normalise_issue_blocked_by_substring_label(jira_config):
    """Substring match: 'blocked-by-dep' should still set is_blocked=True."""
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    raw = _make_raw_issue(labels=["blocked-by-dep"])
    result = _normalise_issue(raw, stale_cutoff, jira_config, 26)
    assert result["is_blocked"] is True


# ---------------------------------------------------------------------------
# get_current_jira_user
# ---------------------------------------------------------------------------

@patch("scripts.standup.gather_jira.requests.get")
def test_get_current_jira_user(mock_get, jira_config):
    mock_get.return_value = MagicMock(ok=True, json=lambda: {"displayName": "Test User", "accountId": "abc"})
    user = get_current_jira_user(jira_config)
    assert user["displayName"] == "Test User"


@patch("scripts.standup.gather_jira.requests.get")
def test_get_current_jira_user_failure(mock_get, jira_config):
    mock_get.return_value = MagicMock(ok=False)
    user = get_current_jira_user(jira_config)
    assert user is None


# ---------------------------------------------------------------------------
# get_active_sprints / get_active_sprint
# ---------------------------------------------------------------------------

@patch("scripts.standup.gather_jira._fetch_project_boards")
@patch("scripts.standup.gather_jira.requests.get")
def test_get_active_sprint_found(mock_get, mock_boards, jira_config):
    """Single scrum board with one active sprint — user has an issue on it."""
    mock_boards.return_value = [{"id": 1, "name": "Board 1", "type": "scrum"}]
    me = {"accountId": "acc-1", "displayName": "Test User"}
    # requests.get is called twice: once for sprint list, once for membership scan
    sprint_resp = MagicMock(ok=True, json=lambda: {
        "values": [{"id": 10, "name": "Sprint 1", "state": "active", "endDate": "2026-07-25"}]
    })
    member_resp = MagicMock(ok=True, json=lambda: {
        "issues": [{"fields": {"assignee": {"accountId": "acc-1", "displayName": "Test User"}}}],
        "total": 1,
    })
    mock_get.side_effect = [sprint_resp, member_resp]
    sprint = get_active_sprint(jira_config, me=me)
    assert sprint is not None
    assert sprint["id"] == 10
    assert sprint["_board_name"] == "Board 1"


@patch("scripts.standup.gather_jira._fetch_project_boards", return_value=[])
def test_get_active_sprint_no_board(mock_boards, jira_config):
    sprint = get_active_sprint(jira_config)
    assert sprint is None


@patch("scripts.standup.gather_jira._fetch_project_boards")
@patch("scripts.standup.gather_jira.requests.get")
def test_get_active_sprints_user_in_two_sprints(mock_get, mock_boards, jira_config):
    """User has issues on two different boards — both sprints returned."""
    me = {"accountId": "acc-1", "displayName": "Test User"}
    mock_boards.return_value = [
        {"id": 1, "name": "Board A", "type": "scrum"},
        {"id": 2, "name": "Board B", "type": "scrum"},
    ]
    issue_with_user = {"fields": {"assignee": {"accountId": "acc-1", "displayName": "Test User"}}}
    mock_get.side_effect = [
        # Board A: sprint list then membership scan
        MagicMock(ok=True, json=lambda: {"values": [{"id": 10, "name": "Sprint A", "state": "active", "endDate": "2026-08-01"}]}),
        MagicMock(ok=True, json=lambda: {"issues": [issue_with_user], "total": 1}),
        # Board B: sprint list then membership scan
        MagicMock(ok=True, json=lambda: {"values": [{"id": 20, "name": "Sprint B", "state": "active", "endDate": "2026-08-01"}]}),
        MagicMock(ok=True, json=lambda: {"issues": [issue_with_user], "total": 1}),
    ]
    sprints = get_active_sprints(jira_config, me=me)
    assert len(sprints) == 2
    ids = {s["id"] for s in sprints}
    assert ids == {10, 20}


@patch("scripts.standup.gather_jira._fetch_project_boards")
@patch("scripts.standup.gather_jira.requests.get")
def test_get_active_sprints_shared_sprint_merged_board_names(mock_get, mock_boards, jira_config):
    """Two boards share the same sprint id — board names are merged."""
    me = {"accountId": "acc-1", "displayName": "Test User"}
    mock_boards.return_value = [
        {"id": 17, "name": "Scope", "type": "scrum"},
        {"id": 19, "name": "AWS & Content", "type": "scrum"},
    ]
    shared_sprint_data = {"id": 42, "name": "Chupacabra 378", "state": "active", "endDate": "2026-08-01"}
    issue_with_user = {"fields": {"assignee": {"accountId": "acc-1", "displayName": "Test User"}}}
    mock_get.side_effect = [
        MagicMock(ok=True, json=lambda: {"values": [shared_sprint_data]}),
        MagicMock(ok=True, json=lambda: {"issues": [issue_with_user], "total": 1}),
        MagicMock(ok=True, json=lambda: {"values": [shared_sprint_data]}),
        MagicMock(ok=True, json=lambda: {"issues": [issue_with_user], "total": 1}),
    ]
    sprints = get_active_sprints(jira_config, me=me)
    # Only one sprint returned (deduplicated by id)
    assert len(sprints) == 1
    assert sprints[0]["id"] == 42
    # Both board names appear in _board_names
    assert "Scope" in sprints[0]["_board_names"]
    assert "AWS & Content" in sprints[0]["_board_names"]


@patch("scripts.standup.gather_jira._fetch_project_boards")
@patch("scripts.standup.gather_jira.requests.get")
def test_get_active_sprints_kanban_boards_skipped(mock_get, mock_boards, jira_config):
    """Kanban boards are skipped; only scrum boards are queried for sprints."""
    me = {"accountId": "acc-1", "displayName": "Test User"}
    mock_boards.return_value = [
        {"id": 1, "name": "Kanban Board", "type": "kanban"},
        {"id": 2, "name": "Scrum Board", "type": "scrum"},
    ]
    issue_with_user = {"fields": {"assignee": {"accountId": "acc-1", "displayName": "Test User"}}}
    mock_get.side_effect = [
        # Only scrum board gets a sprint list call + membership scan
        MagicMock(ok=True, json=lambda: {"values": [{"id": 99, "name": "Sprint X", "state": "active", "endDate": "2026-08-01"}]}),
        MagicMock(ok=True, json=lambda: {"issues": [issue_with_user], "total": 1}),
    ]
    sprints = get_active_sprints(jira_config, me=me)
    # Only 2 GET calls (sprint list + membership) — kanban board skipped
    assert mock_get.call_count == 2
    assert sprints[0]["_board_name"] == "Scrum Board"


# ---------------------------------------------------------------------------
# team_filter <no value> handling
# ---------------------------------------------------------------------------

@patch("scripts.standup.gather_jira.get_standup_messages", return_value=[])
@patch("scripts.standup.gather_jira.get_open_prs", return_value=[])
@patch("scripts.standup.gather_jira.search_my_open_issues", return_value=[])
@patch("scripts.standup.gather_jira.get_sprint_issues")
@patch("scripts.standup.gather_jira.get_active_sprints")
@patch("scripts.standup.gather_jira.get_current_jira_user")
def test_main_no_value_team_filter_treated_as_empty(
    mock_user, mock_sprints, mock_issues, mock_my, mock_prs, mock_slack,
    tmp_workspace, monkeypatch,
):
    """STANDUP_TEAM_MEMBERS='<no value>' must NOT filter out all issues."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token123")
    monkeypatch.setenv("JIRA_PROJECT", "PROJ")
    monkeypatch.setenv("STANDUP_TEAM_MEMBERS", "<no value>")

    mock_user.return_value = {"displayName": "Test User"}
    mock_sprints.return_value = [{"id": 10, "name": "Sprint 1", "state": "active",
                                  "endDate": "2026-08-01", "_board_names": ["My Board"],
                                  "_board_name": "My Board"}]
    mock_issues.return_value = [_make_raw_issue("PROJ-1", "Alice"), _make_raw_issue("PROJ-2", "Bob")]

    from scripts.standup.gather_jira import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0

    signals = json.loads((tmp_workspace / "signals.json").read_text())
    assert len(signals["issues"]) == 2


# ---------------------------------------------------------------------------
# get_sprint_issues
# ---------------------------------------------------------------------------

@patch("scripts.standup.gather_jira.requests.get")
def test_get_sprint_issues(mock_get, jira_config):
    mock_get.return_value = MagicMock(ok=True, json=lambda: {
        "issues": [_make_raw_issue("PROJ-1"), _make_raw_issue("PROJ-2")]
    })
    issues = get_sprint_issues(jira_config, sprint_id=10)
    assert len(issues) == 2


# ---------------------------------------------------------------------------
# search_open_issues (fallback when no active sprint)
# ---------------------------------------------------------------------------

@patch("scripts.standup.gather_jira.requests.get")
def test_search_open_issues(mock_get, jira_config):
    mock_get.return_value = MagicMock(ok=True, json=lambda: {
        "issues": [_make_raw_issue("PROJ-3", "Charlie")]
    })
    issues = search_open_issues(jira_config)
    assert len(issues) == 1
    assert issues[0]["key"] == "PROJ-3"


@patch("scripts.standup.gather_jira.requests.get")
def test_search_open_issues_error(mock_get, jira_config):
    mock_get.return_value = MagicMock(ok=False, status_code=400, text="bad request")
    issues = search_open_issues(jira_config)
    assert issues == []


# ---------------------------------------------------------------------------
# get_open_prs (Bitbucket — now in bb_helpers)
# ---------------------------------------------------------------------------

@patch("scripts.standup.bb_helpers.requests.get")
def test_get_open_prs(mock_get, tmp_workspace):
    config = {
        "WORKSPACE_DIR": str(tmp_workspace),
        "BITBUCKET_WORKSPACE": "myworkspace",
        "BITBUCKET_REPO": "myrepo",
        "BITBUCKET_USERNAME": "user@example.com",
        "BITBUCKET_TOKEN": "token",
    }
    mock_get.return_value = MagicMock(ok=True, json=lambda: {
        "values": [
            {
                "id": 1,
                "title": "Add feature",
                "author": {"display_name": "Alice"},
                "created_on": "2026-07-14T10:00:00Z",
                "reviewers": [],
                "links": {"html": {"href": "https://bb.org/pr/1"}},
            }
        ],
        "next": None,
    })
    prs = get_open_prs(config)
    assert len(prs) == 1
    assert prs[0]["title"] == "Add feature"
    assert prs[0]["age_hours"] > 0


def test_get_open_prs_no_credentials(tmp_workspace):
    config = {"WORKSPACE_DIR": str(tmp_workspace)}
    prs = get_open_prs(config)
    assert prs == []


# ---------------------------------------------------------------------------
# main (integration-style with all externals mocked)
# ---------------------------------------------------------------------------

@patch("scripts.standup.gather_jira.get_standup_messages", return_value=[])
@patch("scripts.standup.gather_jira.get_open_prs", return_value=[])
@patch("scripts.standup.gather_jira.search_my_open_issues", return_value=[])
@patch("scripts.standup.gather_jira.get_sprint_issues")
@patch("scripts.standup.gather_jira.get_active_sprints")
@patch("scripts.standup.gather_jira.get_current_jira_user")
def test_main_with_sprint(
    mock_user, mock_sprints, mock_issues, mock_my, mock_prs, mock_slack,
    jira_config, tmp_workspace, monkeypatch,
):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token123")
    monkeypatch.setenv("JIRA_PROJECT", "PROJ")

    mock_user.return_value = {"displayName": "Test User"}
    mock_sprints.return_value = [{"id": 10, "name": "Sprint 1", "state": "active",
                                  "endDate": "2026-07-25", "_board_names": ["My Board"],
                                  "_board_name": "My Board"}]
    mock_issues.return_value = [_make_raw_issue("PROJ-1"), _make_raw_issue("PROJ-2", "Bob")]

    from scripts.standup.gather_jira import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0

    signals = json.loads((tmp_workspace / "signals.json").read_text())
    assert signals["tracker"] == "jira"
    assert len(signals["issues"]) == 2

    result = json.loads((tmp_workspace / "gather_result.json").read_text())
    assert result["status"] == "DONE"
    assert result["issue_count"] == 2


@patch("scripts.standup.gather_jira.get_standup_messages", return_value=[])
@patch("scripts.standup.gather_jira.get_open_prs", return_value=[])
@patch("scripts.standup.gather_jira.search_my_open_issues", return_value=[])
@patch("scripts.standup.gather_jira.get_sprint_issues")
@patch("scripts.standup.gather_jira.get_active_sprints")
@patch("scripts.standup.gather_jira.get_current_jira_user")
def test_main_with_multiple_sprints(
    mock_user, mock_sprints, mock_issues, mock_my, mock_prs, mock_slack,
    jira_config, tmp_workspace, monkeypatch,
):
    """Issues from two sprints are merged; duplicates are deduped."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token123")
    monkeypatch.setenv("JIRA_PROJECT", "PROJ")

    mock_user.return_value = {"displayName": "Test User"}
    mock_sprints.return_value = [
        {"id": 10, "name": "Sprint A", "state": "active", "endDate": "2026-08-01",
         "_board_id": 1, "_board_names": ["Board A"], "_board_name": "Board A"},
        {"id": 20, "name": "Sprint B", "state": "active", "endDate": "2026-08-01",
         "_board_id": 2, "_board_names": ["Board B"], "_board_name": "Board B"},
    ]
    mock_issues.side_effect = [
        [_make_raw_issue("PROJ-1", "Alice")],
        [_make_raw_issue("PROJ-1", "Alice"), _make_raw_issue("PROJ-3", "Bob")],
    ]

    from scripts.standup.gather_jira import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0

    signals = json.loads((tmp_workspace / "signals.json").read_text())
    keys = [i["key"] for i in signals["issues"]]
    assert keys.count("PROJ-1") == 1  # deduped
    assert "PROJ-3" in keys
    assert len(signals["issues"]) == 2


@patch("scripts.standup.gather_jira.get_standup_messages", return_value=[])
@patch("scripts.standup.gather_jira.get_open_prs", return_value=[])
@patch("scripts.standup.gather_jira.search_my_open_issues", return_value=[])
@patch("scripts.standup.gather_jira.get_sprint_issues")
@patch("scripts.standup.gather_jira.get_active_sprints")
@patch("scripts.standup.gather_jira.get_current_jira_user")
def test_main_shared_sprint_id_fetched_once(
    mock_user, mock_sprints, mock_issues, mock_my, mock_prs, mock_slack,
    jira_config, tmp_workspace, monkeypatch,
):
    """Two entries with the same sprint id → issues fetched once, both boards in all_sprints."""
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token123")
    monkeypatch.setenv("JIRA_PROJECT", "PROJ")

    mock_user.return_value = {"displayName": "Test User"}
    # get_active_sprints already dedupes by sprint id and puts board names in _board_names
    mock_sprints.return_value = [
        {"id": 42, "name": "Chupacabra 378", "state": "active", "endDate": "2026-08-01",
         "_board_names": ["Scope", "AWS & Content"], "_board_name": "Scope"},
    ]
    mock_issues.return_value = [_make_raw_issue("PROJ-1", "Alice"), _make_raw_issue("PROJ-2", "Bob")]

    from scripts.standup.gather_jira import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0

    assert mock_issues.call_count == 1

    signals = json.loads((tmp_workspace / "signals.json").read_text())
    board_str = signals["all_sprints"][0]["board"]
    assert "Scope" in board_str
    assert "AWS & Content" in board_str
    assert len(signals["issues"]) == 2


@patch("scripts.standup.gather_jira.get_standup_messages", return_value=[])
@patch("scripts.standup.gather_jira.get_open_prs", return_value=[])
@patch("scripts.standup.gather_jira.search_my_open_issues", return_value=[])
@patch("scripts.standup.gather_jira.search_open_issues")
@patch("scripts.standup.gather_jira.get_active_sprints", return_value=[])
@patch("scripts.standup.gather_jira.get_current_jira_user")
def test_main_no_sprint_fallback(
    mock_user, mock_sprints, mock_search, mock_my, mock_prs, mock_slack,
    tmp_workspace, monkeypatch,
):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "test@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token123")
    monkeypatch.setenv("JIRA_PROJECT", "PROJ")

    mock_user.return_value = {"displayName": "Test User"}
    mock_search.return_value = [_make_raw_issue("PROJ-3", "Charlie")]

    from scripts.standup.gather_jira import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0

    signals = json.loads((tmp_workspace / "signals.json").read_text())
    assert signals["sprint"] == {}
    assert len(signals["issues"]) == 1
