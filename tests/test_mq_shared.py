"""Tests for scripts/mq/_shared.py — resolve_pr_number and tracker-agnostic helpers."""

from unittest.mock import MagicMock, patch

from scripts.mq._shared import (
    _bb_find_pr_by_jira_key,
    apply_repo_override,
    fetch_changed_files_from_diff,
    is_branch_or_tag,
    label_pr,
    parse_pr_ref,
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

    # URL-override tests: repo_url takes priority over DEFAULT_TRACKER config.
    # Root cause of the Go-project-runs-npx bug: DEFAULT_TRACKER=jira caused
    # clone_by_tracker to clone the Bitbucket repo (cribl/cribl, ~3872 Node.js
    # tests) instead of the GitHub repo specified via --repo.
    def test_github_url_overrides_jira_config(self):
        cfg = {"DEFAULT_TRACKER": "jira"}
        assert resolve_tracker(cfg, repo_url="https://github.com/bhatti/formicary") == "github"

    def test_github_url_overrides_bitbucket_config(self):
        cfg = {"DEFAULT_TRACKER": "bitbucket"}
        assert resolve_tracker(cfg, repo_url="https://github.com/org/repo") == "github"

    def test_bitbucket_url_overrides_github_config(self):
        cfg = {"DEFAULT_TRACKER": "github"}
        assert resolve_tracker(cfg, repo_url="https://bitbucket.org/ws/repo") == "bitbucket"

    def test_empty_url_falls_back_to_config(self):
        cfg = {"DEFAULT_TRACKER": "jira"}
        assert resolve_tracker(cfg, repo_url="") == "bitbucket"

    def test_no_url_falls_back_to_config(self):
        cfg = {"DEFAULT_TRACKER": "jira"}
        assert resolve_tracker(cfg) == "bitbucket"

    def test_unrecognized_url_falls_back_to_config(self):
        cfg = {"DEFAULT_TRACKER": "jira"}
        assert resolve_tracker(cfg, repo_url="https://gitlab.com/org/repo") == "bitbucket"


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

    @patch("scripts.mq._shared.run_cmd")
    def test_passes_cwd_to_run_cmd(self, mock_cmd):
        # Regression: run_cmd must receive cwd= so git runs in the repo directory,
        # not the script's working directory.  Before the fix, run_cmd() did not
        # accept cwd and raised TypeError: unexpected keyword argument 'cwd'.
        mock_cmd.return_value = MagicMock(stdout="5\t2\tsrc/foo.py\n")
        fetch_changed_files_from_diff("/my/repo", "main")
        _, kwargs = mock_cmd.call_args
        assert kwargs.get("cwd") == "/my/repo", (
            "run_cmd must be called with cwd='/my/repo' so git diff runs "
            "inside the cloned repo, not the container's working directory"
        )

    @patch("scripts.mq._shared.run_cmd")
    def test_fallback_also_passes_cwd(self, mock_cmd):
        # The fallback call (origin/base) must also forward cwd.
        mock_cmd.side_effect = [
            Exception("no local ref"),
            MagicMock(stdout="1\t0\tREADME.md\n"),
        ]
        fetch_changed_files_from_diff("/my/repo", "develop")
        fallback_kwargs = mock_cmd.call_args_list[1][1]
        assert fallback_kwargs.get("cwd") == "/my/repo"


class TestLabelPrSkipsBranch:
    @patch("scripts.mq._shared.run_cmd")
    def test_skips_for_branch(self, mock_cmd):
        label_pr({"DEFAULT_TRACKER": "github"}, "feature/x", "scope:api")
        mock_cmd.assert_not_called()

    @patch("scripts.mq._shared.run_cmd")
    def test_runs_for_pr_number(self, mock_cmd):
        label_pr({"DEFAULT_TRACKER": "github", "GH_ORG": "o", "GH_REPO": "r"}, "42", "scope:api")
        mock_cmd.assert_called_once()


class TestParsePrRef:
    def test_github_full_url(self):
        num, repo = parse_pr_ref("https://github.com/org/myrepo/pull/24")
        assert num == "24"
        assert repo == "https://github.com/org/myrepo.git"

    def test_bitbucket_full_url(self):
        num, repo = parse_pr_ref("https://bitbucket.org/ws/myrepo/pull-requests/456/overview")
        assert num == "456"
        assert repo == "https://bitbucket.org/ws/myrepo.git"

    def test_bare_number_passthrough(self):
        num, repo = parse_pr_ref("42")
        assert num == "42"
        assert repo is None

    def test_branch_passthrough(self):
        num, repo = parse_pr_ref("feature/my-branch")
        assert num == "feature/my-branch"
        assert repo is None

    def test_jira_key_passthrough(self):
        num, repo = parse_pr_ref("PROJ-123")
        assert num == "PROJ-123"
        assert repo is None

    def test_github_url_with_trailing_slash(self):
        num, repo = parse_pr_ref("https://github.com/my-org/repo-name/pull/99/")
        assert num == "99"
        assert repo == "https://github.com/my-org/repo-name.git"

    def test_bitbucket_pull_request_singular(self):
        num, repo = parse_pr_ref("https://bitbucket.org/ws/repo/pull-request/7")
        assert num == "7"
        assert repo == "https://bitbucket.org/ws/repo.git"


class TestApplyRepoOverride:
    def test_github_sets_org_and_repo(self):
        config = {}
        apply_repo_override(config, "https://github.com/my-org/my-repo.git")
        assert config["GH_ORG"] == "my-org"
        assert config["GH_REPO"] == "my-repo"

    def test_github_sets_default_tracker(self):
        config = {}
        apply_repo_override(config, "https://github.com/org/repo.git")
        assert config["DEFAULT_TRACKER"] == "github"

    def test_github_does_not_overwrite_existing_tracker(self):
        config = {"DEFAULT_TRACKER": "jira"}
        apply_repo_override(config, "https://github.com/org/repo.git")
        assert config["DEFAULT_TRACKER"] == "jira"

    def test_bitbucket_sets_workspace_and_repo(self):
        config = {}
        apply_repo_override(config, "https://bitbucket.org/my-ws/my-repo.git")
        assert config["BITBUCKET_WORKSPACE"] == "my-ws"
        assert config["BITBUCKET_REPO"] == "my-repo"

    def test_empty_url_is_noop(self):
        config = {"GH_ORG": "existing"}
        apply_repo_override(config, "")
        assert config["GH_ORG"] == "existing"

    def test_unrecognized_url_is_noop(self):
        config = {"GH_ORG": "existing"}
        apply_repo_override(config, "https://gitlab.com/org/repo.git")
        assert config.get("GH_ORG") == "existing"
        assert "BITBUCKET_WORKSPACE" not in config

    def test_full_pr_url_works(self):
        config = {}
        _, repo_url = parse_pr_ref("https://github.com/bhatti/todo-sample/pull/24")
        apply_repo_override(config, repo_url or "")
        assert config["GH_ORG"] == "bhatti"
        assert config["GH_REPO"] == "todo-sample"
