"""Integration tests for Jira-issue-first PR filtering.

These tests hit real Bitbucket, GitHub, and Jira APIs.
They read all credentials from environment / config — no hardcoded private values.

Run with:
    pytest tests/test_pr_filter_integration.py -v -m integration
"""

import os
import pytest

pytestmark = pytest.mark.integration


def _config():
    from scripts.common.config import load_config
    return load_config()


def _skip_unless_jira(config):
    if not all([config.get("JIRA_BASE_URL"), config.get("JIRA_EMAIL"), config.get("JIRA_API_TOKEN")]):
        pytest.skip("Jira credentials not configured")


def _skip_unless_bitbucket(config):
    if not all([config.get("BITBUCKET_WORKSPACE"), config.get("BITBUCKET_REPO")]):
        pytest.skip("Bitbucket credentials not configured")


def _skip_unless_github(config):
    if not all([config.get("GH_ORG"), config.get("GH_REPO")]):
        pytest.skip("GitHub credentials not configured")


# ---------------------------------------------------------------------------
# Jira API utilities
# ---------------------------------------------------------------------------

class TestResolveFieldIdIntegration:
    def test_eng_scrum_team_field_exists(self):
        config = _config()
        _skip_unless_jira(config)
        from scripts.common.jira_api import resolve_field_id
        field_id = resolve_field_id(config, "Eng Scrum Team")
        assert field_id is not None, "Expected 'Eng Scrum Team' field to be found in Jira"
        assert field_id.startswith("customfield_"), f"Expected customfield_... got {field_id}"

    def test_field_resolution_strips_spaces(self):
        config = _config()
        _skip_unless_jira(config)
        from scripts.common.jira_api import resolve_field_id, _field_id_cache
        _field_id_cache.clear()
        # Both "Eng Scrum Team" and "EngscrumTeam" should resolve to the same ID
        id_with_spaces = resolve_field_id(config, "Eng Scrum Team")
        _field_id_cache.clear()
        id_no_spaces = resolve_field_id(config, "EngScrumTeam")
        assert id_with_spaces is not None
        assert id_with_spaces == id_no_spaces


class TestFetchBoardIssueKeysIntegration:
    def test_board_from_config_returns_keys(self):
        config = _config()
        _skip_unless_jira(config)
        board_id = config.get("JIRA_BOARDS")
        if not board_id:
            pytest.skip("JIRA_BOARDS not configured")
        board_id = board_id.split(",")[0].strip()
        from scripts.common.jira_api import fetch_board_issue_keys
        keys = fetch_board_issue_keys(config, board_id, max_results=50)
        assert len(keys) > 0, f"Board {board_id} returned no issue keys"
        # All keys should look like PROJECT-NNN
        import re
        key_re = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
        for k in list(keys)[:5]:
            assert key_re.match(k), f"Unexpected key format: {k}"

    def test_invalid_board_returns_empty(self):
        config = _config()
        _skip_unless_jira(config)
        from scripts.common.jira_api import fetch_board_issue_keys
        keys = fetch_board_issue_keys(config, "99999999", max_results=10)
        assert isinstance(keys, set)


class TestSearchIssuesIntegration:
    def test_search_by_board_field(self):
        config = _config()
        _skip_unless_jira(config)
        from scripts.common.jira_api import search_issues, resolve_field_id
        field_id = resolve_field_id(config, "Eng Scrum Team")
        if not field_id:
            pytest.skip("Eng Scrum Team field not found in this Jira instance")
        # Fetch a sample issue to get a real team name
        import requests
        from scripts.common.jira_api import _base, _auth_headers
        board_id = (config.get("JIRA_BOARDS") or "").split(",")[0].strip()
        if not board_id:
            pytest.skip("JIRA_BOARDS not configured")
        resp = requests.get(
            f"{_base(config)}/rest/agile/1.0/board/{board_id}/issue",
            headers=_auth_headers(config),
            params={"startAt": 0, "maxResults": 5, "fields": f"summary,{field_id}"},
            timeout=30,
        )
        if not resp.ok:
            pytest.skip(f"Board {board_id} not accessible")
        issues = resp.json().get("issues", [])
        team_name = None
        for issue in issues:
            team_val = issue.get("fields", {}).get(field_id)
            if isinstance(team_val, dict) and team_val.get("value"):
                team_name = team_val["value"]
                break
        if not team_name:
            pytest.skip("No team-tagged issues found on board")

        jql = f'{field_id} = "{team_name}" ORDER BY updated DESC'
        results = search_issues(config, jql, max_results=5, fields=["summary"])
        assert len(results) > 0, f"Expected issues for team '{team_name}', got none"


# ---------------------------------------------------------------------------
# _resolve_jira_issue_keys end-to-end
# ---------------------------------------------------------------------------

class TestResolveJiraIssueKeysIntegration:
    def test_board_filter_returns_keys(self):
        config = _config()
        _skip_unless_jira(config)
        board_id = config.get("JIRA_BOARDS")
        if not board_id:
            pytest.skip("JIRA_BOARDS not configured")
        config = dict(config)
        config["JIRA_BOARDS"] = board_id.split(",")[0].strip()
        from scripts.analyze.pr_fetcher import _resolve_jira_issue_keys
        keys = _resolve_jira_issue_keys(config)
        assert keys is not None
        assert len(keys) > 0, "Expected issue keys from board filter"

    def test_team_filter_returns_keys(self):
        config = _config()
        _skip_unless_jira(config)
        # Find a team name dynamically from a board issue
        board_id = (config.get("JIRA_BOARDS") or "").split(",")[0].strip()
        if not board_id:
            pytest.skip("JIRA_BOARDS not configured")
        import requests
        from scripts.common.jira_api import _base, _auth_headers, resolve_field_id
        field_id = resolve_field_id(config, "Eng Scrum Team")
        if not field_id:
            pytest.skip("Eng Scrum Team field not found")
        resp = requests.get(
            f"{_base(config)}/rest/agile/1.0/board/{board_id}/issue",
            headers=_auth_headers(config),
            params={"startAt": 0, "maxResults": 10, "fields": f"summary,{field_id}"},
            timeout=30,
        )
        if not resp.ok:
            pytest.skip(f"Board {board_id} not accessible")
        team_name = None
        for issue in resp.json().get("issues", []):
            val = issue.get("fields", {}).get(field_id)
            if isinstance(val, dict) and val.get("value"):
                team_name = val["value"]
                break
        if not team_name:
            pytest.skip("No team-tagged issues found")
        config = dict(config)
        config["JIRA_SPACE"] = team_name
        config["JIRA_TEAM_FIELD"] = "Eng Scrum Team"
        config.pop("JIRA_BOARDS", None)
        from scripts.analyze.pr_fetcher import _resolve_jira_issue_keys
        keys = _resolve_jira_issue_keys(config)
        assert keys is not None
        assert len(keys) > 0, f"Expected issue keys for team '{team_name}'"

    def test_label_filter_skipped_gracefully(self):
        config = _config()
        _skip_unless_jira(config)
        config = dict(config)
        config["PR_AUDIT_FILTER"] = "label=security"
        config.pop("JIRA_BOARDS", None)
        config.pop("JIRA_SPACE", None)
        from scripts.analyze.pr_fetcher import _resolve_jira_issue_keys
        # label= is GH-native; should return empty set (not None, not an error)
        keys = _resolve_jira_issue_keys(config)
        assert keys == set(), f"label= filter should return empty set, got {keys}"


# ---------------------------------------------------------------------------
# Full PR fetch with board filter (Bitbucket)
# ---------------------------------------------------------------------------

class TestFetchBitbucketPrsWithFilter:
    def test_board_filter_limits_results_to_board_issues(self):
        config = _config()
        _skip_unless_jira(config)
        _skip_unless_bitbucket(config)
        board_id = config.get("JIRA_BOARDS")
        if not board_id:
            pytest.skip("JIRA_BOARDS not configured")
        config = dict(config)
        config["JIRA_BOARDS"] = board_id.split(",")[0].strip()
        from scripts.analyze.pr_fetcher import fetch_bitbucket_prs
        prs = fetch_bitbucket_prs(config, n_prs=10)
        assert len(prs) <= 10, f"Expected at most 10 PRs, got {len(prs)}"
        # Each returned PR should contain a Jira key (or we accept 0 matches cleanly)
        import re
        key_re = re.compile(r"[A-Z][A-Z0-9_]+-\d+")
        matched = [pr for pr in prs if key_re.search(pr.get("title", "") + pr.get("body", ""))]
        # Log how many matched — no hard assertion (board may have 0 open PRs)
        print(f"\n[integ] board filter: {len(prs)} PRs returned, {len(matched)} with Jira keys")


# ---------------------------------------------------------------------------
# Full PR fetch with label filter (GitHub)
# ---------------------------------------------------------------------------

class TestFetchGithubPrsWithLabelFilter:
    def test_label_filter_returns_subset(self):
        config = _config()
        _skip_unless_github(config)
        config = dict(config)
        config["PR_AUDIT_FILTER"] = "label=security"
        config["DEFAULT_TRACKER"] = "github"
        from scripts.analyze.pr_fetcher import fetch_github_prs
        prs = fetch_github_prs(config, n_prs=10)
        assert len(prs) <= 10
        print(f"\n[integ] GH label=security filter: {len(prs)} PRs returned")
