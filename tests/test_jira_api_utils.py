"""Tests for shared Jira API utilities: resolve_field_id, fetch_board_issue_keys, search_issues."""

import pytest
from unittest.mock import patch, MagicMock

from scripts.common import jira_api as _mod
from scripts.common.jira_api import resolve_field_id, fetch_board_issue_keys, search_issues, resolve_current_user_team


FAKE_CONFIG = {
    "JIRA_BASE_URL": "https://jira.example.com",
    "JIRA_EMAIL": "user@example.com",
    "JIRA_API_TOKEN": "token123",
}


def _make_response(json_data=None, status_code=200, ok=True):
    resp = MagicMock()
    resp.ok = ok and (200 <= status_code < 300)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = ""
    return resp


@pytest.fixture(autouse=True)
def clear_field_cache():
    _mod._field_id_cache.clear()
    yield
    _mod._field_id_cache.clear()


class TestResolveFieldId:
    def test_found_by_exact_name(self):
        fields = [
            {"id": "customfield_10100", "name": "Sprint"},
            {"id": "customfield_10200", "name": "TeamField"},
        ]
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _make_response(fields)
            result = resolve_field_id(FAKE_CONFIG, "TeamField")
        assert result == "customfield_10200"

    def test_found_strips_spaces(self):
        fields = [{"id": "customfield_10248", "name": "Eng Scrum Team"}]
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _make_response(fields)
            # "EngScrumTeam" should match "Eng Scrum Team" after space-stripping
            result = resolve_field_id(FAKE_CONFIG, "EngScrumTeam")
        assert result == "customfield_10248"

    def test_found_case_insensitive(self):
        fields = [{"id": "customfield_10248", "name": "Eng Scrum Team"}]
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _make_response(fields)
            result = resolve_field_id(FAKE_CONFIG, "eng scrum team")
        assert result == "customfield_10248"

    def test_not_found_returns_none(self):
        fields = [{"id": "customfield_10100", "name": "Sprint"}]
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _make_response(fields)
            result = resolve_field_id(FAKE_CONFIG, "NonExistentField")
        assert result is None

    def test_api_error_returns_none(self):
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _make_response(status_code=500, ok=False)
            result = resolve_field_id(FAKE_CONFIG, "SomeField")
        assert result is None

    def test_cache_hit_skips_api(self):
        fields = [{"id": "customfield_10200", "name": "TeamField"}]
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _make_response(fields)
            result1 = resolve_field_id(FAKE_CONFIG, "TeamField")
            assert result1 == "customfield_10200"
            assert mock_req.get.call_count == 1

            # Second call should use cache — no additional API call
            result2 = resolve_field_id(FAKE_CONFIG, "TeamField")
            assert result2 == "customfield_10200"
            assert mock_req.get.call_count == 1  # still 1


def _sprint_response(sprint_id: int, state: str = "active", days_ago: int | None = None) -> dict:
    """Build a fake sprint dict for test fixtures."""
    sp: dict = {"id": sprint_id, "state": state}
    if days_ago is not None:
        from datetime import datetime, timezone, timedelta
        sp["completeDate"] = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return sp


def _count_resp(total: int) -> object:
    """Fake sprint count response (first pagination call)."""
    return _make_response({"total": total, "values": []})


class TestFetchBoardIssueKeys:
    def test_returns_keys_from_active_sprint(self):
        """Active sprint issues are collected via sprint/{id}/issue endpoint."""
        sprints_page = {"values": [_sprint_response(10, state="active")]}
        sprint_issues = {"issues": [{"key": "PROJ-1"}, {"key": "PROJ-2"}], "total": 2}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.side_effect = [
                _count_resp(1),                  # board/sprint?maxResults=1 (count)
                _make_response(sprints_page),    # board/sprint?startAt=0 (data)
                _make_response(sprint_issues),   # sprint/10/issue
            ]
            keys = fetch_board_issue_keys(FAKE_CONFIG, 42, max_results=100)
        assert keys == {"PROJ-1", "PROJ-2"}

    def test_includes_done_issues_from_recent_closed_sprint(self):
        """Closed sprint issues (including Done) are included when sprint ended within days_back."""
        sprints_page = {"values": [_sprint_response(20, state="closed", days_ago=14)]}
        sprint_issues = {"issues": [{"key": "PROJ-10"}, {"key": "PROJ-11"}], "total": 2}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.side_effect = [
                _count_resp(1),
                _make_response(sprints_page),
                _make_response(sprint_issues),
            ]
            keys = fetch_board_issue_keys(FAKE_CONFIG, 42, days_back=90)
        assert keys == {"PROJ-10", "PROJ-11"}

    def test_uses_all_sprints_when_none_are_recent(self):
        """When all sprints are older than days_back, they are still fetched as fallback."""
        sprints_page = {"values": [_sprint_response(30, state="closed", days_ago=120)]}
        sprint_issues = {"issues": [{"key": "PROJ-30"}], "total": 1}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.side_effect = [
                _count_resp(1),
                _make_response(sprints_page),
                _make_response(sprint_issues),
            ]
            keys = fetch_board_issue_keys(FAKE_CONFIG, 42, days_back=90)
        assert "PROJ-30" in keys

    def test_api_error_falls_back_to_board_endpoint(self):
        """Sprint count API 404 falls back to board/issue endpoint."""
        board_issues = {"issues": [{"key": "PROJ-99"}], "total": 1}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.side_effect = [
                _make_response(status_code=404, ok=False),  # count call fails
                _make_response(board_issues),               # board/issue fallback
            ]
            keys = fetch_board_issue_keys(FAKE_CONFIG, 99)
        assert "PROJ-99" in keys

    def test_no_sprints_falls_back_to_board_endpoint(self):
        """Board with no sprints (kanban) falls back to board/issue endpoint."""
        board_issues = {"issues": [{"key": "PROJ-55"}], "total": 1}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.side_effect = [
                _count_resp(0),                # total=0
                _make_response({"values": []}),  # data call returns no sprints
                _make_response(board_issues),    # board/issue fallback
            ]
            keys = fetch_board_issue_keys(FAKE_CONFIG, 0)
        assert "PROJ-55" in keys

    def test_jumps_to_last_page_for_old_boards(self):
        """When board has many historical sprints, startAt skips to the last page."""
        # Board has 120 sprints; start_at should be max(0, 120-50)=70
        sprints_page = {"values": [_sprint_response(119, state="active")]}
        sprint_issues = {"issues": [{"key": "NEW-1"}], "total": 1}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.side_effect = [
                _count_resp(120),
                _make_response(sprints_page),
                _make_response(sprint_issues),
            ]
            keys = fetch_board_issue_keys(FAKE_CONFIG, 42)
        # Verify the data call used startAt=70
        data_call_params = mock_req.get.call_args_list[1][1]["params"]
        assert data_call_params["startAt"] == 70
        assert "NEW-1" in keys


class TestResolveCurrentUserTeam:
    @pytest.fixture(autouse=True)
    def clear_user_cache(self):
        _mod._user_team_cache.clear()
        yield
        _mod._user_team_cache.clear()

    def test_returns_team_from_assigned_issue(self):
        field_list = [{"id": "customfield_10248", "name": "Eng Scrum Team"}]
        team_issues = {"issues": [{"key": "PROJ-1", "fields": {"customfield_10248": {"value": "TeamAlpha"}}}]}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _make_response(field_list)
            mock_req.post.return_value = _make_response(team_issues)
            result = resolve_current_user_team(FAKE_CONFIG)
        assert result == "TeamAlpha"
        # uses currentUser() in JQL — no /myself call needed
        assert mock_req.get.call_count == 1  # only the field-list lookup

    def test_returns_none_when_search_fails(self):
        field_list = [{"id": "customfield_10248", "name": "Eng Scrum Team"}]
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _make_response(field_list)
            mock_req.post.return_value = _make_response(status_code=401, ok=False)
            result = resolve_current_user_team(FAKE_CONFIG)
        assert result is None

    def test_returns_none_when_no_assigned_issues(self):
        field_list = [{"id": "customfield_10248", "name": "Eng Scrum Team"}]
        empty_issues = {"issues": []}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _make_response(field_list)
            mock_req.post.return_value = _make_response(empty_issues)
            result = resolve_current_user_team(FAKE_CONFIG)
        assert result is None

    def test_returns_none_when_team_field_not_found(self):
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _make_response([])  # empty field list
            result = resolve_current_user_team(FAKE_CONFIG)
        assert result is None
        mock_req.post.assert_not_called()  # search never reached

    def test_caches_result(self):
        field_list = [{"id": "customfield_10248", "name": "Eng Scrum Team"}]
        team_issues = {"issues": [{"key": "PROJ-1", "fields": {"customfield_10248": {"value": "TeamBeta"}}}]}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _make_response(field_list)
            mock_req.post.return_value = _make_response(team_issues)
            result1 = resolve_current_user_team(FAKE_CONFIG)
            result2 = resolve_current_user_team(FAKE_CONFIG)
        assert result1 == result2 == "TeamBeta"
        # Second call hits _user_team_cache → no additional API calls
        assert mock_req.post.call_count == 1


class TestSearchIssues:
    def test_uses_post_method(self):
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.post.return_value = _make_response({"issues": [{"key": "PROJ-10"}]})
            issues = search_issues(FAKE_CONFIG, 'project = "PROJ"', max_results=5)
        assert len(issues) == 1
        assert mock_req.post.called

    def test_custom_fields_passed_in_body(self):
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.post.return_value = _make_response({"issues": []})
            search_issues(FAKE_CONFIG, "project = X", fields=["summary", "customfield_10248"])
            call_kwargs = mock_req.post.call_args
            body = call_kwargs[1]["json"]
            assert "customfield_10248" in body["fields"]

    def test_api_error_returns_empty(self):
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.post.return_value = _make_response(status_code=400, ok=False)
            result = search_issues(FAKE_CONFIG, "project = X")
        assert result == []
