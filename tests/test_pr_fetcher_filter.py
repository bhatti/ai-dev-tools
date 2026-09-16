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
    _supplement_from_dev_status,
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
        # When auto-detect returns None, falls back to board sprint fetch
        count_resp = {"total": 1, "values": []}
        sprints_page = {"values": [{"id": 10, "state": "active"}]}
        sprint_issues = {"issues": [{"key": "PROJ-1"}, {"key": "PROJ-2"}], "total": 2}
        with patch("scripts.analyze.pr_fetcher.resolve_current_user_team", return_value=None):
            with patch("scripts.common.jira_api.requests") as mock_req:
                mock_req.get.side_effect = [
                    _mock_response(count_resp),    # board/sprint?maxResults=1 (count)
                    _mock_response(sprints_page),  # board/sprint?startAt=0 (data)
                    _mock_response(sprint_issues), # sprint/10/issue
                ]
                config = {**JIRA_CONFIG, "JIRA_BOARDS": "42"}
                keys, issues = _resolve_jira_issue_keys(config)
        assert keys == {"PROJ-1", "PROJ-2"}

    def test_board_filter_uses_team_when_auto_detected(self):
        # When auto-detect returns a team name, JQL team filter is used instead of sprint fetch
        field_list = [{"id": "customfield_10248", "name": "Eng Scrum Team"}]
        team_issues = {"issues": [{"key": "PROJ-5", "id": "5"}, {"key": "PROJ-6", "id": "6"}]}
        with patch("scripts.analyze.pr_fetcher.resolve_current_user_team", return_value="TeamAlpha"):
            with patch("scripts.common.jira_api.requests") as mock_req:
                mock_req.get.return_value = _mock_response(field_list)
                mock_req.post.return_value = _mock_response(team_issues)
                config = {**JIRA_CONFIG, "JIRA_BOARDS": "42"}
                keys, issues = _resolve_jira_issue_keys(config)
        assert keys == {"PROJ-5", "PROJ-6"}
        assert len(issues) == 2
        # JQL post was called (team filter), not board/sprint endpoint
        assert mock_req.post.called

    def test_team_filter_uses_jql(self):
        field_list = [{"id": "customfield_10248", "name": "Eng Scrum Team"}]
        search_result = {"issues": [{"key": "PROJ-10", "id": "10"}, {"key": "PROJ-11", "id": "11"}]}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _mock_response(field_list)
            mock_req.post.return_value = _mock_response(search_result)
            config = {**JIRA_CONFIG, "JIRA_SPACE": TEAM, "JIRA_TEAM_FIELD": "Eng Scrum Team"}
            keys, issues = _resolve_jira_issue_keys(config)
        assert {"PROJ-10", "PROJ-11"} == keys
        assert len(issues) == 2
        post_body = mock_req.post.call_args[1]["json"]
        assert "customfield_10248" in post_body["jql"]
        assert TEAM in post_body["jql"]

    def test_explicit_filter_uses_jql(self):
        field_list = [{"id": "customfield_10100", "name": "Priority Area"}]
        search_result = {"issues": [{"key": "FEAT-5", "id": "5"}]}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _mock_response(field_list)
            mock_req.post.return_value = _mock_response(search_result)
            config = {**JIRA_CONFIG, "PR_AUDIT_FILTER": "Priority Area=Backend"}
            keys, issues = _resolve_jira_issue_keys(config)
        assert "FEAT-5" in keys

    def test_board_with_team_skips_sprint_fetch(self):
        # When JIRA_SPACE (team) is set, board sprint fetch is skipped in favor of JQL team filter.
        # This prevents false positives from hundreds of keys on shared/cross-team boards.
        field_list = [{"id": "customfield_10248", "name": "Eng Scrum Team"}]
        search_result = {"issues": [{"key": "PROJ-2", "id": "2"}]}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _mock_response(field_list)
            mock_req.post.return_value = _mock_response(search_result)
            config = {**JIRA_CONFIG, "JIRA_BOARDS": "1", "JIRA_SPACE": TEAM}
            keys, issues = _resolve_jira_issue_keys(config)
        # Only team JQL results — sprint fetch never ran
        assert "PROJ-2" in keys
        # Verify JQL was called (team filter) and no board/sprint endpoint was hit
        assert mock_req.post.called
        get_urls = [str(call) for call in mock_req.get.call_args_list]
        assert not any("board/1/sprint" in u for u in get_urls)

    def test_team_field_not_found_returns_empty_set(self):
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _mock_response([])  # empty field list
            config = {**JIRA_CONFIG, "JIRA_SPACE": TEAM}
            keys, issues = _resolve_jira_issue_keys(config)
        assert keys == set()
        assert issues == []

    def test_label_filter_not_sent_to_jira(self):
        # label=X is GH-native; _resolve_jira_issue_keys returns None so caller uses GH label path
        with patch("scripts.common.jira_api.requests") as mock_req:
            config = {**JIRA_CONFIG, "PR_AUDIT_FILTER": "label=security"}
            keys = _resolve_jira_issue_keys(config)
        # No API calls — label filter bypasses Jira lookup entirely
        mock_req.get.assert_not_called()
        mock_req.post.assert_not_called()
        assert keys is None


# ---------------------------------------------------------------------------
# End-to-end: resolve then filter
# ---------------------------------------------------------------------------

class TestResolveAndFilter:
    def test_full_pipeline(self):
        field_list = [{"id": "customfield_10248", "name": "Eng Scrum Team"}]
        search_result = {"issues": [{"key": "PROJ-5", "id": "5"}, {"key": "PROJ-6", "id": "6"}]}
        with patch("scripts.common.jira_api.requests") as mock_req:
            mock_req.get.return_value = _mock_response(field_list)
            mock_req.post.return_value = _mock_response(search_result)
            config = {**JIRA_CONFIG, "JIRA_SPACE": TEAM}
            prs = [
                {"title": "PROJ-5 add feature", "body": "", "branch": "main"},
                {"title": "PROJ-7 other team PR", "body": "", "branch": "main"},
                {"title": "no key PR", "body": "", "branch": "main"},
            ]
            issue_keys, issues = _resolve_jira_issue_keys(config)
            filtered = _filter_prs_by_issue_keys(prs, issue_keys)
        assert len(filtered) == 1
        assert filtered[0]["title"] == "PROJ-5 add feature"

    def test_branch_name_matches_issue_key(self):
        prs = [
            {"title": "fix bug", "body": "", "branch": "goatbot/bugs/PROJ-42_description"},
            {"title": "other fix", "body": "", "branch": "feature/unrelated"},
        ]
        filtered = _filter_prs_by_issue_keys(prs, {"PROJ-42"})
        assert len(filtered) == 1
        assert filtered[0]["branch"] == "goatbot/bugs/PROJ-42_description"


# ---------------------------------------------------------------------------
# _supplement_from_dev_status
# ---------------------------------------------------------------------------

class TestSupplementFromDevStatus:
    def test_adds_prs_from_dev_status(self):
        config = {**JIRA_CONFIG, "BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "repo"}
        prs = [{"number": 100, "title": "existing PR"}]
        jira_issues = [{"key": "PROJ-1", "id": "1001"}]
        linked = [{"url": "https://bitbucket.org/ws/repo/pull-requests/200", "status": "MERGED"}]
        extra_pr = {"number": 200, "title": "discovered PR"}
        with patch("scripts.analyze.pr_fetcher.get_jira_linked_prs", return_value=linked), \
             patch("scripts.analyze.pr_fetcher.fetch_prs_by_numbers", return_value=[extra_pr]):
            result = _supplement_from_dev_status(config, jira_issues, prs)
        assert len(result) == 2
        assert result[1]["number"] == 200

    def test_skips_already_fetched_prs(self):
        config = {**JIRA_CONFIG}
        prs = [{"number": 200, "title": "already here"}]
        jira_issues = [{"key": "PROJ-1", "id": "1001"}]
        linked = [{"url": "https://bitbucket.org/ws/repo/pull-requests/200", "status": "MERGED"}]
        with patch("scripts.analyze.pr_fetcher.get_jira_linked_prs", return_value=linked), \
             patch("scripts.analyze.pr_fetcher.fetch_prs_by_numbers") as mock_fetch:
            result = _supplement_from_dev_status(config, jira_issues, prs)
        mock_fetch.assert_not_called()
        assert len(result) == 1

    def test_noop_when_no_jira_issues(self):
        prs = [{"number": 1}]
        result = _supplement_from_dev_status({}, [], prs)
        assert result is prs

    def test_caps_at_50_issues(self):
        config = {**JIRA_CONFIG}
        jira_issues = [{"key": f"PROJ-{i}", "id": str(i)} for i in range(100)]
        with patch("scripts.analyze.pr_fetcher.get_jira_linked_prs", return_value=[]) as mock_linked:
            _supplement_from_dev_status(config, jira_issues, [])
        assert mock_linked.call_count == 50

    def test_handles_github_pr_urls(self):
        config = {**JIRA_CONFIG, "GH_ORG": "org", "GH_REPO": "repo"}
        jira_issues = [{"key": "PROJ-1", "id": "1"}]
        linked = [{"url": "https://github.com/org/repo/pull/42"}]
        extra_pr = {"number": 42, "title": "GH PR"}
        with patch("scripts.analyze.pr_fetcher.get_jira_linked_prs", return_value=linked), \
             patch("scripts.analyze.pr_fetcher.fetch_prs_by_numbers", return_value=[extra_pr]):
            result = _supplement_from_dev_status(config, jira_issues, [])
        assert len(result) == 1
        assert result[0]["number"] == 42
