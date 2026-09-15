"""Tests for Jira-issue-first PR filtering in pr_fetcher.py.

All team/field names use generic placeholders — no private org names.
"""

import pytest
from unittest.mock import patch, MagicMock

from scripts.common import jira_api as _jira_mod
from scripts.analyze.pr_fetcher import (
    _filter_by_label,
    _filter_prs_by_issue_keys,
    _resolve_jira_issue_keys,
)


JIRA_CONFIG = {
    "JIRA_BASE_URL": "https://jira.example.com",
    "JIRA_EMAIL": "user@example.com",
    "JIRA_API_TOKEN": "tok",
}

TEAM = "TeamAlpha"   # generic placeholder — never a private org/team name


def _mock_response(json_data=None, status_code=200, ok=True):
    resp = MagicMock()
    resp.ok = ok and (200 <= status_code < 300)
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = ""
    return resp


@pytest.fixture(autouse=True)
def clear_field_cache():
    _jira_mod._field_id_cache.clear()
    yield
    _jira_mod._field_id_cache.clear()


# ---------------------------------------------------------------------------
# _filter_prs_by_issue_keys
# ---------------------------------------------------------------------------

class TestFilterPrsByIssueKeys:
    def test_pr_with_key_kept(self):
        prs = [{"title": "PROJ-42 fix bug", "body": ""}]
        assert _filter_prs_by_issue_keys(prs, {"PROJ-42", "PROJ-99"}) == prs

    def test_pr_without_key_excluded(self):
        prs = [{"title": "fix bug", "body": "no jira key here"}]
        assert _filter_prs_by_issue_keys(prs, {"PROJ-42"}) == []

    def test_pr_with_wrong_key_excluded(self):
        prs = [{"title": "PROJ-1 fix", "body": ""}]
        assert _filter_prs_by_issue_keys(prs, {"PROJ-99"}) == []

    def test_key_in_body(self):
        prs = [{"title": "fixes stuff", "body": "see PROJ-7 for context"}]
        assert _filter_prs_by_issue_keys(prs, {"PROJ-7"}) == prs

    def test_empty_issue_keys_excludes_all(self, capsys):
        prs = [{"title": "PROJ-1 fix", "body": ""}]
        assert _filter_prs_by_issue_keys(prs, set()) == []
        capsys.readouterr()  # consume output

    def test_empty_prs_returns_empty(self):
        assert _filter_prs_by_issue_keys([], {"PROJ-1"}) == []

    def test_second_key_in_title_matches(self):
        # re.findall must check ALL keys, not just the first one
        prs = [{"title": "PROJ-1 and PROJ-7 fix", "body": ""}]
        assert _filter_prs_by_issue_keys(prs, {"PROJ-7"}) == prs

    def test_key_in_body_only_matches(self):
        prs = [{"title": "misc fix", "body": "closes PROJ-99"}]
        assert _filter_prs_by_issue_keys(prs, {"PROJ-99"}) == prs

    def test_empty_issue_keys_returns_empty_with_warning(self, capsys):
        prs = [{"title": "PROJ-1 fix", "body": ""}]
        result = _filter_prs_by_issue_keys(prs, set())
        assert result == []
        out = capsys.readouterr().out
        assert "WARNING" in out


# ---------------------------------------------------------------------------
# _filter_by_label
# ---------------------------------------------------------------------------

class TestFilterByLabel:
    def test_label_match_kept(self):
        prs = [{"title": "PR 1", "body": "", "labels": ["security", "bug"]}]
        assert _filter_by_label(prs, "security") == prs

    def test_no_match_excluded(self):
        prs = [{"title": "PR 1", "body": "", "labels": ["enhancement"]}]
        assert _filter_by_label(prs, "security") == []

    def test_case_insensitive(self):
        prs = [{"title": "PR 1", "body": "", "labels": ["Security"]}]
        assert _filter_by_label(prs, "security") == prs

    def test_empty_labels(self):
        prs = [{"title": "PR 1", "body": "", "labels": []}]
        assert _filter_by_label(prs, "security") == []

    def test_no_labels_key(self):
        prs = [{"title": "PR 1", "body": ""}]
        assert _filter_by_label(prs, "security") == []


# ---------------------------------------------------------------------------
# _resolve_jira_issue_keys
# ---------------------------------------------------------------------------

class TestResolveJiraIssueKeys:
    def test_no_config_returns_none(self):
        config = {**JIRA_CONFIG}
        assert _resolve_jira_issue_keys(config) is None

    def test_missing_creds_returns_none(self):
        config = {"JIRA_BOARDS": "42"}
        assert _resolve_jira_issue_keys(config) is None

    def test_board_filter_returns_keys(self):
        # New flow: count call → board/sprint data → sprint/issue (includes Done issues)
        count_resp = {"total": 1, "values": []}
        sprints_page = {"values": [{"id": 10, "state": "active"}]}
        sprint_issues = {"issues": [{"key": "PROJ-1"}, {"key": "PROJ-2"}], "total": 2}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.side_effect = [
                _mock_response(count_resp),    # board/sprint?maxResults=1 (count)
                _mock_response(sprints_page),  # board/sprint?startAt=0 (data)
                _mock_response(sprint_issues), # sprint/10/issue
            ]
            config = {**JIRA_CONFIG, "JIRA_BOARDS": "42"}
            keys = _resolve_jira_issue_keys(config)
        assert keys == {"PROJ-1", "PROJ-2"}

    def test_team_filter_uses_jql(self):
        field_list = [{"id": "customfield_10248", "name": "Eng Scrum Team"}]
        search_result = {"issues": [{"key": "PROJ-10"}, {"key": "PROJ-11"}]}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _mock_response(field_list)
            mock_req.post.return_value = _mock_response(search_result)
            config = {**JIRA_CONFIG, "JIRA_SPACE": TEAM, "JIRA_TEAM_FIELD": "Eng Scrum Team"}
            keys = _resolve_jira_issue_keys(config)
        assert {"PROJ-10", "PROJ-11"} == keys
        post_body = mock_req.post.call_args[1]["json"]
        assert "customfield_10248" in post_body["jql"]
        assert TEAM in post_body["jql"]

    def test_explicit_filter_uses_jql(self):
        field_list = [{"id": "customfield_10100", "name": "Priority Area"}]
        search_result = {"issues": [{"key": "FEAT-5"}]}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _mock_response(field_list)
            mock_req.post.return_value = _mock_response(search_result)
            config = {**JIRA_CONFIG, "PR_AUDIT_FILTER": "Priority Area=Backend"}
            keys = _resolve_jira_issue_keys(config)
        assert "FEAT-5" in keys

    def test_multiple_sources_unioned(self):
        # Board filter uses count+data sprint fetch; team filter uses JQL
        count_resp = {"total": 1, "values": []}
        sprints_page = {"values": [{"id": 10, "state": "active"}]}
        sprint_issues = {"issues": [{"key": "PROJ-1"}], "total": 1}
        field_list = [{"id": "customfield_10248", "name": "Eng Scrum Team"}]
        search_result = {"issues": [{"key": "PROJ-2"}]}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.side_effect = [
                _mock_response(count_resp),    # count call
                _mock_response(sprints_page),  # data call
                _mock_response(sprint_issues), # sprint/10/issue
                _mock_response(field_list),    # field resolution for team filter
            ]
            mock_req.post.return_value = _mock_response(search_result)
            config = {**JIRA_CONFIG, "JIRA_BOARDS": "1", "JIRA_SPACE": TEAM}
            keys = _resolve_jira_issue_keys(config)
        assert "PROJ-1" in keys
        assert "PROJ-2" in keys

    def test_team_field_not_found_returns_empty_set(self):
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _mock_response([])  # empty field list
            config = {**JIRA_CONFIG, "JIRA_SPACE": TEAM}
            keys = _resolve_jira_issue_keys(config)
        assert keys == set()

    def test_label_filter_not_sent_to_jira(self):
        # label=X is for GH-native path; _resolve_jira_issue_keys should skip it
        with patch("scripts.common.jira_api.requests") as mock_req:
            config = {**JIRA_CONFIG, "PR_AUDIT_FILTER": "label=security"}
            keys = _resolve_jira_issue_keys(config)
        # No API calls should be made
        mock_req.get.assert_not_called()
        mock_req.post.assert_not_called()
        assert keys == set()


# ---------------------------------------------------------------------------
# End-to-end: resolve then filter
# ---------------------------------------------------------------------------

class TestResolveAndFilter:
    def test_full_pipeline(self):
        field_list = [{"id": "customfield_10248", "name": "Eng Scrum Team"}]
        search_result = {"issues": [{"key": "PROJ-5"}, {"key": "PROJ-6"}]}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _mock_response(field_list)
            mock_req.post.return_value = _mock_response(search_result)
            config = {**JIRA_CONFIG, "JIRA_SPACE": TEAM}
            prs = [
                {"title": "PROJ-5 add feature", "body": ""},
                {"title": "PROJ-7 other team PR", "body": ""},
                {"title": "no key PR", "body": ""},
            ]
            issue_keys = _resolve_jira_issue_keys(config)
            filtered = _filter_prs_by_issue_keys(prs, issue_keys)
        assert len(filtered) == 1
        assert filtered[0]["title"] == "PROJ-5 add feature"
