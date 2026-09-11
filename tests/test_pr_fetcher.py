"""Tests for scripts/analyze/pr_fetcher.py"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from scripts.analyze.pr_fetcher import (
    KNOWN_BOTS,
    _fetch_single_bb_pr,
    _fetch_single_gh_pr,
    build_pr_context,
    classify_comments,
    fetch_github_prs,
    fetch_prs_by_numbers,
    fetch_single_pr,
    link_pr_to_issue,
    parse_pr_url,
)


# ---------------------------------------------------------------------------
# classify_comments
# ---------------------------------------------------------------------------

class TestClassifyComments:
    def test_empty(self):
        result = classify_comments([])
        assert result["bot_comments"] == []
        assert result["human_comments"] == []
        assert result["ci_comments"] == []
        assert result["review_bot_comments"] == []

    def test_known_bot(self):
        comments = [
            {"author": "dependabot[bot]", "body": "bump version", "type": "comment"},
            {"author": "alice", "body": "lgtm", "type": "review"},
        ]
        result = classify_comments(comments)
        assert len(result["bot_comments"]) == 1
        assert result["bot_comments"][0]["author"] == "dependabot[bot]"
        assert len(result["human_comments"]) == 1
        assert result["human_comments"][0]["author"] == "alice"

    def test_bot_suffix(self):
        comments = [
            {"author": "my-custom-bot", "body": "auto comment", "type": "comment"},
        ]
        result = classify_comments(comments)
        assert len(result["bot_comments"]) == 1

    def test_bot_bracket_suffix(self):
        comments = [
            {"author": "codecov[bot]", "body": "coverage report", "type": "comment"},
        ]
        result = classify_comments(comments)
        assert len(result["bot_comments"]) == 1

    def test_all_human(self):
        comments = [
            {"author": "alice", "body": "nice work", "type": "review"},
            {"author": "bob", "body": "one nit", "type": "review"},
        ]
        result = classify_comments(comments)
        assert len(result["bot_comments"]) == 0
        assert len(result["human_comments"]) == 2

    def test_empty_author(self):
        comments = [
            {"author": "", "body": "anonymous", "type": "comment"},
        ]
        result = classify_comments(comments)
        # Empty author is not a bot
        assert len(result["human_comments"]) == 1


# ---------------------------------------------------------------------------
# link_pr_to_issue
# ---------------------------------------------------------------------------

class TestLinkPrToIssue:
    def test_jira_in_title(self):
        pr = {"title": "PROJ-123: fix login bug", "body": ""}
        config = {"JIRA_BASE_URL": "https://jira.example.com"}
        result = link_pr_to_issue(pr, config)
        assert result is not None
        assert result["key"] == "PROJ-123"
        assert result["source"] == "jira"
        assert "jira.example.com/browse/PROJ-123" in result["url"]

    def test_github_closes_keyword(self):
        pr = {"title": "Fix bug", "body": "Closes #42"}
        config = {"GH_ORG": "org", "GH_REPO": "repo"}
        result = link_pr_to_issue(pr, config)
        assert result is not None
        assert result["key"] == "#42"
        assert result["source"] == "github"

    def test_github_fixes_keyword(self):
        pr = {"title": "Fix bug", "body": "fixes #99"}
        config = {"GH_ORG": "org", "GH_REPO": "repo"}
        result = link_pr_to_issue(pr, config)
        assert result is not None
        assert result["key"] == "#99"

    def test_bare_issue_reference(self):
        pr = {"title": "Related to #7", "body": ""}
        config = {"GH_ORG": "org", "GH_REPO": "repo"}
        result = link_pr_to_issue(pr, config)
        assert result is not None
        assert result["key"] == "#7"

    def test_no_issue_reference(self):
        pr = {"title": "Refactor code", "body": "Just cleaning up"}
        config = {}
        result = link_pr_to_issue(pr, config)
        assert result is None

    def test_jira_takes_precedence(self):
        # If both JIRA key and GitHub issue ref exist, JIRA wins (searched first)
        pr = {"title": "PROJ-10: fix #5", "body": ""}
        config = {"JIRA_BASE_URL": "https://jira.example.com", "GH_ORG": "org", "GH_REPO": "repo"}
        result = link_pr_to_issue(pr, config)
        assert result["key"] == "PROJ-10"
        assert result["source"] == "jira"


# ---------------------------------------------------------------------------
# build_pr_context
# ---------------------------------------------------------------------------

class TestBuildPrContext:
    def test_empty_prs(self):
        result = build_pr_context([])
        assert "no PRs available" in result

    def test_access_denied_issue_shows_unknown_not_false(self):
        """access_denied issues must show 'unknown' not 'false' so auditor doesn't count them as missing AC."""
        prs = [{
            "number": 10,
            "title": "Restricted project PR",
            "author": "dev",
            "merged_at": "2024-01-15",
            "branch": "feat/x",
            "files_changed": 5,
            "review_decision": "APPROVED",
            "linked_issue": {
                "key": "RESTRICTED-42",
                "source": "jira",
                "details": {
                    "access_denied": True,
                    "title": "",
                    "body": "",
                    "labels": [],
                    "acceptance_criteria": "",
                    "has_acceptance_criteria": None,
                    "design_doc_links": [],
                },
            },
            "human_comments": [],
            "bot_comments": [],
            "ci_comments": [],
            "review_bot_comments": [],
            "body": "",
        }]
        result = build_pr_context(prs)
        assert "access denied" in result.lower()
        assert "has_acceptance_criteria**: false" not in result

    def test_has_ac_true_without_text(self):
        """has_acceptance_criteria=True should render as true even if ac text is empty."""
        prs = [{
            "number": 11,
            "title": "Feature with AC",
            "author": "dev",
            "merged_at": "2024-01-15",
            "branch": "feat/y",
            "files_changed": 2,
            "review_decision": "",
            "linked_issue": {
                "key": "PROJ-1",
                "source": "jira",
                "details": {
                    "title": "Some ticket",
                    "body": "stuff",
                    "labels": [],
                    "acceptance_criteria": "",  # empty — edge case
                    "has_acceptance_criteria": True,
                    "design_doc_links": [],
                },
            },
            "human_comments": [],
            "bot_comments": [],
            "ci_comments": [],
            "review_bot_comments": [],
            "body": "",
        }]
        result = build_pr_context(prs)
        assert "has_acceptance_criteria**: true" in result

    def test_single_pr(self):
        prs = [{
            "number": 1,
            "title": "Add feature",
            "author": "alice",
            "merged_at": "2024-01-15",
            "branch": "feature/add",
            "files_changed": 3,
            "review_decision": "APPROVED",
            "linked_issue": {"key": "#10", "source": "github"},
            "human_comments": [{"author": "bob", "body": "looks good"}],
            "bot_comments": [],
            "body": "This adds a new feature",
        }]
        result = build_pr_context(prs)
        assert "PR #1" in result
        assert "Add feature" in result
        assert "alice" in result
        assert "APPROVED" in result
        assert "#10" in result
        assert "bob" in result

    def test_budget_truncation(self):
        prs = []
        for i in range(100):
            prs.append({
                "number": i,
                "title": f"PR title {i} " + "x" * 200,
                "author": "user",
                "merged_at": "2024-01-01",
                "branch": f"branch-{i}",
                "files_changed": 1,
                "review_decision": "",
                "linked_issue": None,
                "human_comments": [],
                "bot_comments": [],
                "body": "Long description " * 50,
            })
        result = build_pr_context(prs, max_chars=5000)
        # All 100 PRs are always included (design intent: never drop PRs).
        # Each PR is truncated to per_pr_budget = max(5000//100, 500) = 500 chars.
        assert result.count("### PR #") == 100
        assert "_(truncated)_" in result  # per-PR truncation markers present

    def test_multiple_prs(self):
        prs = [
            {"number": 1, "title": "First", "author": "a", "merged_at": "",
             "branch": "", "files_changed": 0, "review_decision": "",
             "linked_issue": None, "human_comments": [], "bot_comments": [], "body": ""},
            {"number": 2, "title": "Second", "author": "b", "merged_at": "",
             "branch": "", "files_changed": 0, "review_decision": "",
             "linked_issue": None, "human_comments": [], "bot_comments": [], "body": ""},
        ]
        result = build_pr_context(prs)
        assert "2 total" in result
        assert "PR #1" in result
        assert "PR #2" in result


# ---------------------------------------------------------------------------
# fetch_github_prs (mocked subprocess)
# ---------------------------------------------------------------------------

class TestFetchGithubPrs:
    @patch("scripts.analyze.pr_fetcher.subprocess.run")
    @patch("scripts.analyze.pr_fetcher._fetch_gh_review_comments", return_value=[])
    def test_basic_fetch(self, mock_review, mock_run):
        gh_output = json.dumps([{
            "number": 42,
            "title": "Fix login",
            "body": "Fixes #10",
            "author": {"login": "alice"},
            "mergedAt": "2024-01-15T10:00:00Z",
            "url": "https://github.com/org/repo/pull/42",
            "headRefName": "fix/login",
            "comments": [],
            "reviews": [],
            "reviewDecision": "APPROVED",
            "labels": [],
            "files": [{"path": "src/login.py"}, {"path": "tests/test_login.py"}],
        }])
        mock_run.return_value = MagicMock(
            returncode=0, stdout=gh_output, stderr="",
        )
        config = {"GH_ORG": "org", "GH_REPO": "repo"}
        prs = fetch_github_prs(config, n_prs=10)
        assert len(prs) == 1
        assert prs[0]["number"] == 42
        assert prs[0]["title"] == "Fix login"
        assert prs[0]["author"] == "alice"
        assert prs[0]["files_changed"] == 2
        assert prs[0]["review_decision"] == "APPROVED"

    @patch("scripts.analyze.pr_fetcher.subprocess.run")
    def test_missing_config(self, mock_run):
        config = {}
        prs = fetch_github_prs(config)
        assert prs == []
        mock_run.assert_not_called()

    @patch("scripts.analyze.pr_fetcher.subprocess.run")
    def test_gh_cli_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        config = {"GH_ORG": "org", "GH_REPO": "repo"}
        prs = fetch_github_prs(config)
        assert prs == []


# ---------------------------------------------------------------------------
# fetch_single_pr / _fetch_single_gh_pr / _fetch_single_bb_pr
# ---------------------------------------------------------------------------

class TestFetchSingleGhPr:
    @patch("scripts.analyze.pr_fetcher._fetch_gh_review_comments", return_value=[])
    @patch("scripts.analyze.pr_fetcher.subprocess.run")
    def test_basic_fetch(self, mock_run, mock_review):
        pr_json = json.dumps({
            "number": 9,
            "title": "AI: create data model",
            "body": "Closes #1",
            "author": {"login": "bhatti"},
            "mergedAt": "2026-06-01T22:44:06Z",
            "url": "https://github.com/bhatti/todo-sample/pull/9",
            "headRefName": "ai/1-create-data-model",
            "comments": [],
            "reviews": [{"author": {"login": "bhatti"}, "state": "APPROVED", "body": ""}],
            "reviewDecision": "APPROVED",
            "labels": [],
            "files": [
                {"path": "internal/model/todo.go", "additions": 50, "deletions": 0},
                {"path": "internal/model/todo_test.go", "additions": 30, "deletions": 0},
            ],
        })
        mock_run.return_value = MagicMock(returncode=0, stdout=pr_json, stderr="")
        config = {"GH_ORG": "bhatti", "GH_REPO": "todo-sample"}
        pr = _fetch_single_gh_pr(config, 9)

        assert pr is not None
        assert pr["number"] == 9
        assert pr["title"] == "AI: create data model"
        assert pr["author"] == "bhatti"
        assert pr["files_changed"] == 2
        assert pr["additions"] == 80
        assert "APPROVED" in pr["approvers"] or pr["review_decision"] == "APPROVED"
        assert "substantive_human_comment_count" in pr
        assert "rubber_stamp_approvers" in pr
        assert "is_bot_authored" in pr

    @patch("scripts.analyze.pr_fetcher.subprocess.run")
    def test_missing_config_returns_none(self, mock_run):
        config = {}
        pr = _fetch_single_gh_pr(config, 9)
        assert pr is None
        mock_run.assert_not_called()

    @patch("scripts.analyze.pr_fetcher.subprocess.run")
    def test_api_error_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
        config = {"GH_ORG": "bhatti", "GH_REPO": "todo-sample"}
        pr = _fetch_single_gh_pr(config, 999)
        assert pr is None


class TestFetchSingleBbPr:
    @patch("scripts.analyze.pr_fetcher._fetch_bb_comments", return_value=[])
    @patch("scripts.analyze.pr_fetcher._fetch_bb_diffstat", return_value=[
        {"path": "src/login.py", "lines_added": 10, "lines_removed": 2},
    ])
    def test_basic_fetch(self, mock_diffstat, mock_comments):
        import requests as _requests
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "id": 45974,
            "title": "CRIBL-44279: Deleting already revoked tokens",
            "description": "Fixes token deletion flow",
            "author": {"display_name": "Goatbot", "nickname": "goatbot"},
            "updated_on": "2026-08-24T15:41:01Z",
            "links": {"html": {"href": "https://bitbucket.org/cribl/cribl/pull-requests/45974"}},
            "source": {"branch": {"name": "goatbot/CRIBL-44279"}},
            "participants": [
                {"user": {"display_name": "Alice"}, "approved": True, "role": "REVIEWER"},
                {"user": {"display_name": "Bob"}, "approved": False, "role": "REVIEWER"},
            ],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.get", return_value=mock_resp):
            config = {
                "BITBUCKET_WORKSPACE": "cribl",
                "BITBUCKET_REPO": "cribl",
                "BITBUCKET_USERNAME": "user",
                "BITBUCKET_TOKEN": "token",
            }
            pr = _fetch_single_bb_pr(config, 45974)

        assert pr is not None
        assert pr["number"] == 45974
        assert pr["title"] == "CRIBL-44279: Deleting already revoked tokens"
        assert pr["author"] == "Goatbot"
        assert pr["is_bot_authored"] is True
        assert pr["approvers"] == ["Alice"]
        assert pr["review_decision"] == "APPROVED"
        assert pr["files_changed"] == 1
        assert pr["additions"] == 10
        assert "substantive_human_comment_count" in pr
        assert "rubber_stamp_approvers" in pr

    def test_missing_config_returns_none(self):
        config = {}
        pr = _fetch_single_bb_pr(config, 45974)
        assert pr is None


class TestFetchSinglePrDispatcher:
    def test_dispatches_to_bitbucket(self):
        config = {
            "DEFAULT_TRACKER": "jira",
            "BITBUCKET_WORKSPACE": "ws",
            "BITBUCKET_REPO": "repo",
        }
        with patch("scripts.analyze.pr_fetcher._fetch_single_bb_pr", return_value={"number": 1}) as mock_bb:
            with patch("scripts.analyze.pr_fetcher._fetch_single_gh_pr") as mock_gh:
                result = fetch_single_pr(config, 1)
                mock_bb.assert_called_once_with(config, 1)
                mock_gh.assert_not_called()
                assert result == {"number": 1}

    def test_dispatches_to_github(self):
        config = {"DEFAULT_TRACKER": "github"}
        with patch("scripts.analyze.pr_fetcher._fetch_single_gh_pr", return_value={"number": 9}) as mock_gh:
            with patch("scripts.analyze.pr_fetcher._fetch_single_bb_pr") as mock_bb:
                result = fetch_single_pr(config, 9)
                mock_gh.assert_called_once_with(config, 9)
                mock_bb.assert_not_called()
                assert result == {"number": 9}

    def test_defaults_to_github_when_tracker_unset(self):
        config = {}
        with patch("scripts.analyze.pr_fetcher._fetch_single_gh_pr", return_value=None) as mock_gh:
            result = fetch_single_pr(config, 1)
            mock_gh.assert_called_once()
            assert result is None


# ---------------------------------------------------------------------------
# parse_pr_url
# ---------------------------------------------------------------------------

class TestParsePrUrl:
    def test_github_url(self):
        tracker, num = parse_pr_url("https://github.com/bhatti/todo-sample/pull/9")
        assert tracker == "github"
        assert num == 9

    def test_github_url_with_trailing_slash(self):
        tracker, num = parse_pr_url("https://github.com/org/repo/pull/123/")
        assert tracker == "github"
        assert num == 123

    def test_bitbucket_url(self):
        tracker, num = parse_pr_url("https://bitbucket.org/cribl/cribl/pull-requests/45974")
        assert tracker == "jira/bitbucket"
        assert num == 45974

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError, match="Unrecognized"):
            parse_pr_url("https://gitlab.com/org/repo/merge_requests/1")

    def test_not_a_url_raises(self):
        with pytest.raises(ValueError):
            parse_pr_url("not-a-url-at-all")


# ---------------------------------------------------------------------------
# fetch_prs_by_numbers
# ---------------------------------------------------------------------------

class TestFetchPrsByNumbers:
    def test_fetches_multiple_gh_prs(self):
        config = {"DEFAULT_TRACKER": "github", "GH_ORG": "org", "GH_REPO": "repo"}
        with patch("scripts.analyze.pr_fetcher._fetch_single_gh_pr") as mock:
            mock.side_effect = [
                {"number": 9, "title": "PR 9"},
                {"number": 10, "title": "PR 10"},
            ]
            prs = fetch_prs_by_numbers(config, [9, 10])
        assert len(prs) == 2
        assert prs[0]["number"] == 9
        assert prs[1]["number"] == 10

    def test_skips_none_results(self):
        config = {"DEFAULT_TRACKER": "github", "GH_ORG": "org", "GH_REPO": "repo"}
        with patch("scripts.analyze.pr_fetcher._fetch_single_gh_pr") as mock:
            mock.side_effect = [{"number": 1, "title": "ok"}, None]
            prs = fetch_prs_by_numbers(config, [1, 2])
        assert len(prs) == 1
        assert prs[0]["number"] == 1

    def test_routes_to_bb_for_jira_tracker(self):
        config = {"DEFAULT_TRACKER": "jira", "BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "repo"}
        with patch("scripts.analyze.pr_fetcher._fetch_single_bb_pr") as mock_bb:
            with patch("scripts.analyze.pr_fetcher._fetch_single_gh_pr") as mock_gh:
                mock_bb.return_value = {"number": 100}
                prs = fetch_prs_by_numbers(config, [100])
        mock_bb.assert_called_once_with(config, 100)
        mock_gh.assert_not_called()
        assert prs[0]["number"] == 100

    def test_empty_list_returns_empty(self):
        prs = fetch_prs_by_numbers({"DEFAULT_TRACKER": "github"}, [])
        assert prs == []

    def test_fetch_single_pr_delegates_to_fetch_prs_by_numbers(self):
        config = {"DEFAULT_TRACKER": "github", "GH_ORG": "org", "GH_REPO": "repo"}
        with patch("scripts.analyze.pr_fetcher._fetch_single_gh_pr") as mock:
            mock.return_value = {"number": 5, "title": "test"}
            result = fetch_single_pr(config, 5)
        assert result == {"number": 5, "title": "test"}
        mock.assert_called_once_with(config, 5)

    def test_fetch_single_pr_returns_none_on_error(self):
        config = {"DEFAULT_TRACKER": "github", "GH_ORG": "org", "GH_REPO": "repo"}
        with patch("scripts.analyze.pr_fetcher._fetch_single_gh_pr", return_value=None):
            result = fetch_single_pr(config, 99)
        assert result is None
