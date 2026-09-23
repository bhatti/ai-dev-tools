"""Tests for scripts/mq/_shared.py — resolve_pr_number and tracker-agnostic helpers."""

from unittest.mock import MagicMock, patch

from scripts.mq._shared import (
    _bb_find_pr_by_jira_key,
    fetch_changed_files_from_diff,
    is_branch_or_tag,
    label_pr,
    resolve_pr_number,
    resolve_tracker,
    repo_slug,
)


class TestResolveTracker:
    def test_github_default(self):
        assert resolve_tracker({}) == "github"

    def test_github_explicit(self):
        assert resolve_tracker({"DEFAULT_TRACKER": "github"}) == "github"

    def test_jira_maps_to_bitbucket(self):
        assert resolve_tracker({"DEFAULT_TRACKER": "jira"}) == "bitbucket"

    def test_bitbucket_direct(self):
        assert resolve_tracker({"DEFAULT_TRACKER": "bitbucket"}) == "bitbucket"


class TestRepoSlug:
    def test_github_slug(self):
        assert repo_slug({"GH_ORG": "org", "GH_REPO": "repo"}) == "org/repo"

    def test_bitbucket_slug(self):
        cfg = {"DEFAULT_TRACKER": "jira", "BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "r"}
        assert repo_slug(cfg) == "ws/r"


class TestResolvePrNumber:
    def test_numeric_passthrough(self):
        assert resolve_pr_number({}, "42") == "42"

    def test_none_input(self):
        assert resolve_pr_number({}, None) is None

    def test_empty_input(self):
        assert resolve_pr_number({}, "") is None

    def test_github_non_numeric_passthrough(self):
        assert resolve_pr_number({"DEFAULT_TRACKER": "github"}, "PROJ-123") == "PROJ-123"

    @patch("scripts.mq._shared._bb_find_pr_by_jira_key", return_value="99")
    def test_jira_key_resolved(self, mock_find):
        cfg = {"DEFAULT_TRACKER": "jira", "BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "r"}
        assert resolve_pr_number(cfg, "PROJ-40913") == "99"
        mock_find.assert_called_once_with(cfg, "PROJ-40913")

    @patch("scripts.mq._shared._bb_find_pr_by_jira_key", return_value=None)
    def test_jira_key_not_found(self, mock_find):
        cfg = {"DEFAULT_TRACKER": "jira", "BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "r"}
        assert resolve_pr_number(cfg, "PROJ-99999") is None

    def test_non_jira_pattern_passthrough(self):
        cfg = {"DEFAULT_TRACKER": "jira"}
        assert resolve_pr_number(cfg, "some-branch") == "some-branch"


class TestBbFindPrByJiraKey:
    @patch("scripts.mq._shared.requests.get")
    def test_found_by_branch(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"values": [{"id": 42}]}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp
        cfg = {"BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "r",
               "BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}
        result = _bb_find_pr_by_jira_key(cfg, "PROJ-40913")
        assert result == "42"
        assert mock_get.call_count == 1

    @patch("scripts.mq._shared.requests.get")
    def test_found_in_merged(self, mock_get):
        empty_resp = MagicMock()
        empty_resp.json.return_value = {"values": []}
        empty_resp.raise_for_status.return_value = None
        merged_resp = MagicMock()
        merged_resp.json.return_value = {"values": [{"id": 77}]}
        merged_resp.raise_for_status.return_value = None
        mock_get.side_effect = [empty_resp, merged_resp]
        cfg = {"BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "r",
               "BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}
        result = _bb_find_pr_by_jira_key(cfg, "PROJ-123")
        assert result == "77"

    @patch("scripts.mq._shared.requests.get")
    def test_found_by_title(self, mock_get):
        empty_resp = MagicMock()
        empty_resp.json.return_value = {"values": []}
        empty_resp.raise_for_status.return_value = None
        title_resp = MagicMock()
        title_resp.json.return_value = {"values": [{"id": 55}]}
        title_resp.raise_for_status.return_value = None
        # open, merged, declined, then title search
        mock_get.side_effect = [empty_resp, empty_resp, empty_resp, title_resp]
        cfg = {"BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "r",
               "BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}
        result = _bb_find_pr_by_jira_key(cfg, "PROJ-456")
        assert result == "55"

    @patch("scripts.mq._shared.requests.get")
    def test_not_found(self, mock_get):
        empty_resp = MagicMock()
        empty_resp.json.return_value = {"values": []}
        empty_resp.raise_for_status.return_value = None
        mock_get.return_value = empty_resp
        cfg = {"BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "r",
               "BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}
        result = _bb_find_pr_by_jira_key(cfg, "PROJ-999")
        assert result is None

    @patch("scripts.mq._shared.requests.get", side_effect=Exception("network"))
    def test_api_error_returns_none(self, mock_get):
        cfg = {"BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "r",
               "BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}
        result = _bb_find_pr_by_jira_key(cfg, "PROJ-123")
        assert result is None


class TestIsBranchOrTag:
    def test_numeric_is_not_branch(self):
        assert is_branch_or_tag("42") is False

    def test_jira_key_is_not_branch(self):
        assert is_branch_or_tag("PROJ-123") is False

    def test_branch_with_slash(self):
        assert is_branch_or_tag("feature/my-branch") is True

    def test_simple_branch(self):
        assert is_branch_or_tag("develop") is True

    def test_tag(self):
        assert is_branch_or_tag("v1.2.3") is True

    def test_main_branch(self):
        assert is_branch_or_tag("main") is True

    def test_empty_is_not_branch(self):
        assert is_branch_or_tag("") is False

    def test_multi_segment_jira(self):
        assert is_branch_or_tag("AB-1") is False

    def test_lowercase_not_jira(self):
        assert is_branch_or_tag("proj-123") is True


class TestFetchChangedFilesFromDiff:
    @patch("scripts.mq._shared.run_cmd")
    def test_parses_numstat(self, mock_cmd):
        mock_cmd.return_value = MagicMock(
            stdout="10\t5\tsrc/main.py\n3\t0\tREADME.md\n"
        )
        files = fetch_changed_files_from_diff("/repo", "main")
        assert len(files) == 2
        assert files[0] == {"path": "src/main.py", "additions": 10, "deletions": 5}
        assert files[1] == {"path": "README.md", "additions": 3, "deletions": 0}

    @patch("scripts.mq._shared.run_cmd")
    def test_binary_file_dashes(self, mock_cmd):
        mock_cmd.return_value = MagicMock(stdout="-\t-\timage.png\n")
        files = fetch_changed_files_from_diff("/repo", "main")
        assert files[0]["additions"] == 0
        assert files[0]["deletions"] == 0

    @patch("scripts.mq._shared.run_cmd")
    def test_empty_output(self, mock_cmd):
        mock_cmd.return_value = MagicMock(stdout="")
        assert fetch_changed_files_from_diff("/repo", "main") == []

    @patch("scripts.mq._shared.run_cmd")
    def test_falls_back_to_origin(self, mock_cmd):
        mock_cmd.side_effect = [
            Exception("no ref"),
            MagicMock(stdout="1\t1\tfile.py\n"),
        ]
        files = fetch_changed_files_from_diff("/repo", "main")
        assert len(files) == 1
        assert mock_cmd.call_count == 2


class TestLabelPrSkipsBranch:
    @patch("scripts.mq._shared.run_cmd")
    def test_skips_for_branch(self, mock_cmd):
        label_pr({"DEFAULT_TRACKER": "github"}, "feature/x", "scope:api")
        mock_cmd.assert_not_called()

    @patch("scripts.mq._shared.run_cmd")
    def test_runs_for_pr_number(self, mock_cmd):
        label_pr({"DEFAULT_TRACKER": "github", "GH_ORG": "o", "GH_REPO": "r"}, "42", "scope:api")
        mock_cmd.assert_called_once()
