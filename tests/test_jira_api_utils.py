"""Tests for shared Jira API utilities: resolve_field_id, fetch_board_issue_keys, search_issues."""

import pytest
from unittest.mock import patch, MagicMock

from scripts.common import jira_api as _mod
from scripts.common.jira_api import resolve_field_id, fetch_board_issue_keys, search_issues


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


def _sprint_response(sprint_id: int, state: str = "active", days_ago: int = 0) -> dict:
    """Build a fake sprint dict for test fixtures."""
    from datetime import datetime, timezone, timedelta
    end = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
    return {"id": sprint_id, "state": state, "completeDate": end}


class TestFetchBoardIssueKeys:
    def test_returns_keys_from_active_sprint(self):
        """Active sprint issues are collected via sprint/{id}/issue endpoint."""
        sprints_resp = {"values": [_sprint_response(10, state="active")]}
        sprint_issues = {"issues": [{"key": "PROJ-1"}, {"key": "PROJ-2"}], "total": 2}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.side_effect = [
                _make_response(sprints_resp),    # board/{id}/sprint
                _make_response(sprint_issues),   # sprint/10/issue
            ]
            keys = fetch_board_issue_keys(FAKE_CONFIG, 42, max_results=100)
        assert keys == {"PROJ-1", "PROJ-2"}

    def test_includes_done_issues_from_recent_closed_sprint(self):
        """Closed sprint issues (including Done) are included when sprint ended within days_back."""
        sprints_resp = {"values": [_sprint_response(20, state="closed", days_ago=14)]}
        sprint_issues = {"issues": [{"key": "PROJ-10"}, {"key": "PROJ-11"}], "total": 2}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.side_effect = [
                _make_response(sprints_resp),
                _make_response(sprint_issues),
            ]
            keys = fetch_board_issue_keys(FAKE_CONFIG, 42, days_back=90)
        assert keys == {"PROJ-10", "PROJ-11"}

    def test_uses_all_sprints_when_none_are_recent(self):
        """When all sprints are older than days_back, they are still fetched as fallback."""
        sprints_resp = {"values": [_sprint_response(30, state="closed", days_ago=120)]}
        sprint_issues = {"issues": [{"key": "PROJ-30"}], "total": 1}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.side_effect = [
                _make_response(sprints_resp),  # board/sprint
                _make_response(sprint_issues), # sprint/30/issue — still fetched
            ]
            keys = fetch_board_issue_keys(FAKE_CONFIG, 42, days_back=90)
        assert "PROJ-30" in keys

    def test_api_error_falls_back_to_board_endpoint(self):
        """Sprint API 404 falls back to board/issue endpoint."""
        board_issues = {"issues": [{"key": "PROJ-99"}], "total": 1}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.side_effect = [
                _make_response(status_code=404, ok=False),  # board/sprint fails
                _make_response(board_issues),               # board/issue fallback
            ]
            keys = fetch_board_issue_keys(FAKE_CONFIG, 99)
        assert "PROJ-99" in keys

    def test_no_sprints_falls_back_to_board_endpoint(self):
        """Board with no sprints (kanban) falls back to board/issue endpoint."""
        sprints_resp = {"values": []}
        board_issues = {"issues": [{"key": "PROJ-55"}], "total": 1}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.side_effect = [
                _make_response(sprints_resp),
                _make_response(board_issues),
            ]
            keys = fetch_board_issue_keys(FAKE_CONFIG, 0)
        assert "PROJ-55" in keys


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
