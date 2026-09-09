"""Tests for scripts/analyze/pr_fetcher.py"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from scripts.analyze.pr_fetcher import (
    KNOWN_BOTS,
    build_pr_context,
    classify_comments,
    fetch_github_prs,
    link_pr_to_issue,
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
