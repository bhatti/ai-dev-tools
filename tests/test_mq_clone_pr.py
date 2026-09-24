"""Tests for scripts/mq/clone_pr.py"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from scripts.mq.clone_pr import _apply_repo_override, main


class TestClonePr:
    @patch("scripts.mq.clone_pr.clone_by_tracker")
    @patch("scripts.mq.clone_pr.load_config")
    @patch("scripts.mq.clone_pr.get_workspace_dir")
    def test_clones_and_checkouts_gh(self, mock_ws, mock_cfg, mock_clone, tmp_path):
        mock_cfg.return_value = {"GH_ORG": "org", "GH_REPO": "repo"}
        mock_ws.return_value = tmp_path
        with patch("scripts.mq.clone_pr.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0)
            result = CliRunner().invoke(main, ["--pr-number", "42"])
        assert result.exit_code == 0
        mock_clone.assert_called_once()
        args = mock_sub.run.call_args[0][0]
        assert "gh" in args[0]
        assert "42" in args

    @patch("scripts.mq.clone_pr.clone_by_tracker")
    @patch("scripts.mq.clone_pr.load_config")
    @patch("scripts.mq.clone_pr.get_workspace_dir")
    def test_skips_if_already_cloned(self, mock_ws, mock_cfg, mock_clone, tmp_path):
        mock_cfg.return_value = {"GH_ORG": "org", "GH_REPO": "repo"}
        mock_ws.return_value = tmp_path
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (repo_dir / ".git").mkdir()
        result = CliRunner().invoke(main, [])
        mock_clone.assert_not_called()
        assert result.exit_code == 0

    @patch("scripts.mq.clone_pr.resolve_pr_number", return_value="7")
    @patch("scripts.mq.clone_pr.subprocess")
    @patch("scripts.mq.clone_pr.clone_by_tracker")
    @patch("scripts.mq.clone_pr.load_config")
    @patch("scripts.mq.clone_pr.get_workspace_dir")
    def test_bitbucket_checkout(self, mock_ws, mock_cfg, mock_clone, mock_sub, mock_resolve, tmp_path):
        mock_cfg.return_value = {
            "DEFAULT_TRACKER": "jira",
            "BITBUCKET_WORKSPACE": "ws",
            "BITBUCKET_REPO": "repo",
        }
        mock_ws.return_value = tmp_path
        mock_sub.run.return_value = MagicMock(returncode=0)
        result = CliRunner().invoke(main, ["--pr-number", "7"])
        assert result.exit_code == 0
        mock_clone.assert_called_once()
        calls = mock_sub.run.call_args_list
        assert len(calls) == 2
        fetch_args = calls[0][0][0]
        assert "git" in fetch_args[0]
        assert "refs/pull-requests/7/from:pr-7" in fetch_args
        checkout_args = calls[1][0][0]
        assert checkout_args == ["git", "checkout", "pr-7"]

    @patch("scripts.mq.clone_pr.resolve_pr_number", return_value="42")
    @patch("scripts.mq.clone_pr.subprocess")
    @patch("scripts.mq.clone_pr.clone_by_tracker")
    @patch("scripts.mq.clone_pr.load_config")
    @patch("scripts.mq.clone_pr.get_workspace_dir")
    def test_bitbucket_jira_key_resolved(self, mock_ws, mock_cfg, mock_clone, mock_sub, mock_resolve, tmp_path):
        mock_cfg.return_value = {
            "DEFAULT_TRACKER": "jira",
            "BITBUCKET_WORKSPACE": "ws",
            "BITBUCKET_REPO": "repo",
        }
        mock_ws.return_value = tmp_path
        mock_sub.run.return_value = MagicMock(returncode=0)
        result = CliRunner().invoke(main, ["--pr-number", "PROJ-40913"])
        assert result.exit_code == 0
        mock_resolve.assert_called_once()
        calls = mock_sub.run.call_args_list
        fetch_args = calls[0][0][0]
        assert "refs/pull-requests/42/from:pr-42" in fetch_args

    @patch("scripts.mq.clone_pr.resolve_pr_number", return_value=None)
    @patch("scripts.mq.clone_pr.subprocess")
    @patch("scripts.mq.clone_pr.clone_by_tracker")
    @patch("scripts.mq.clone_pr.load_config")
    @patch("scripts.mq.clone_pr.get_workspace_dir")
    def test_bitbucket_jira_key_not_found(self, mock_ws, mock_cfg, mock_clone, mock_sub, mock_resolve, tmp_path):
        mock_cfg.return_value = {
            "DEFAULT_TRACKER": "jira",
            "BITBUCKET_WORKSPACE": "ws",
            "BITBUCKET_REPO": "repo",
        }
        mock_ws.return_value = tmp_path
        result = CliRunner().invoke(main, ["--pr-number", "PROJ-99999"])
        assert result.exit_code == 0
        mock_sub.run.assert_not_called()

    @patch("scripts.mq.clone_pr.clone_by_tracker")
    @patch("scripts.mq.clone_pr.load_config")
    @patch("scripts.mq.clone_pr.get_workspace_dir")
    def test_no_checkout_without_pr_number(self, mock_ws, mock_cfg, mock_clone, tmp_path):
        mock_cfg.return_value = {"GH_ORG": "org", "GH_REPO": "repo"}
        mock_ws.return_value = tmp_path
        with patch("scripts.mq.clone_pr.subprocess") as mock_sub:
            result = CliRunner().invoke(main, [])
        assert result.exit_code == 0
        mock_sub.run.assert_not_called()

    @patch("scripts.mq.clone_pr.clone_by_tracker")
    @patch("scripts.mq.clone_pr.load_config")
    @patch("scripts.mq.clone_pr.get_workspace_dir")
    def test_branch_checkout(self, mock_ws, mock_cfg, mock_clone, tmp_path):
        mock_cfg.return_value = {"GH_ORG": "org", "GH_REPO": "repo"}
        mock_ws.return_value = tmp_path
        with patch("scripts.mq.clone_pr.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0)
            result = CliRunner().invoke(main, ["--pr-number", "feature/billing-v2"])
        assert result.exit_code == 0
        mock_clone.assert_called_once()
        calls = mock_sub.run.call_args_list
        assert calls[0][0][0] == ["git", "fetch", "origin", "feature/billing-v2"]
        assert calls[1][0][0] == ["git", "checkout", "feature/billing-v2"]

    @patch("scripts.mq.clone_pr.clone_by_tracker")
    @patch("scripts.mq.clone_pr.load_config")
    @patch("scripts.mq.clone_pr.get_workspace_dir")
    def test_tag_checkout(self, mock_ws, mock_cfg, mock_clone, tmp_path):
        mock_cfg.return_value = {"GH_ORG": "org", "GH_REPO": "repo"}
        mock_ws.return_value = tmp_path
        with patch("scripts.mq.clone_pr.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0)
            result = CliRunner().invoke(main, ["--pr-number", "v1.2.3"])
        assert result.exit_code == 0
        calls = mock_sub.run.call_args_list
        assert calls[0][0][0] == ["git", "fetch", "origin", "v1.2.3"]
        assert calls[1][0][0] == ["git", "checkout", "v1.2.3"]

    @patch("scripts.mq.clone_pr.clone_by_tracker")
    @patch("scripts.mq.clone_pr.load_config")
    @patch("scripts.mq.clone_pr.get_workspace_dir")
    def test_branch_checkout_fallback_to_origin(self, mock_ws, mock_cfg, mock_clone, tmp_path):
        mock_cfg.return_value = {"GH_ORG": "org", "GH_REPO": "repo"}
        mock_ws.return_value = tmp_path
        with patch("scripts.mq.clone_pr.subprocess") as mock_sub:
            mock_sub.run.side_effect = [
                MagicMock(returncode=0),   # fetch succeeds
                MagicMock(returncode=1),   # checkout fails
                MagicMock(returncode=0),   # fallback to origin/branch
            ]
            result = CliRunner().invoke(main, ["--pr-number", "develop"])
        assert result.exit_code == 0
        calls = mock_sub.run.call_args_list
        assert calls[2][0][0] == ["git", "checkout", "origin/develop"]


class TestTrackerOverrideFromRepoUrl:
    """Regression: DEFAULT_TRACKER=jira must not override an explicit --repo GitHub URL.

    Bug: with DEFAULT_TRACKER=jira, resolve_tracker() returned "bitbucket", so
    clone_by_tracker cloned cribl/cribl (~3872 Node.js tests) instead of the
    GitHub repo passed via --repo, causing project_type=node for a Go project.
    """

    @patch("scripts.mq.clone_pr.clone_by_tracker")
    @patch("scripts.mq.clone_pr.load_config")
    @patch("scripts.mq.clone_pr.get_workspace_dir")
    def test_github_url_wins_over_jira_default_tracker(self, mock_ws, mock_cfg, mock_clone, tmp_path):
        mock_cfg.return_value = {
            "DEFAULT_TRACKER": "jira",
            "GH_ORG": "bhatti",
            "GH_REPO": "formicary",
        }
        mock_ws.return_value = tmp_path
        result = CliRunner().invoke(main, ["--repo", "https://github.com/bhatti/formicary"])
        assert result.exit_code == 0
        mock_clone.assert_called_once()
        _, _, tracker = mock_clone.call_args[0]
        assert tracker == "github", (
            "When --repo is a github.com URL, tracker must be 'github' even if "
            "DEFAULT_TRACKER=jira in pod env"
        )


class TestApplyRepoOverride:
    def test_github_url(self):
        cfg = {"GH_ORG": "old", "GH_REPO": "old"}
        _apply_repo_override(cfg, "https://github.com/bhatti/formicary")
        assert cfg["GH_ORG"] == "bhatti"
        assert cfg["GH_REPO"] == "formicary"

    def test_bitbucket_url(self):
        cfg = {}
        _apply_repo_override(cfg, "https://bitbucket.org/myws/myrepo")
        assert cfg["BITBUCKET_WORKSPACE"] == "myws"
        assert cfg["BITBUCKET_REPO"] == "myrepo"

    def test_empty_url_noop(self):
        cfg = {"GH_ORG": "org", "GH_REPO": "repo"}
        _apply_repo_override(cfg, "")
        assert cfg["GH_ORG"] == "org"

    def test_none_url_noop(self):
        cfg = {"GH_ORG": "org"}
        _apply_repo_override(cfg, None)
        assert cfg["GH_ORG"] == "org"


class TestRepoAndBranchFlags:
    @patch("scripts.mq.clone_pr.clone_by_tracker")
    @patch("scripts.mq.clone_pr.load_config")
    @patch("scripts.mq.clone_pr.get_workspace_dir")
    def test_repo_flag_overrides_config(self, mock_ws, mock_cfg, mock_clone, tmp_path):
        mock_cfg.return_value = {"GH_ORG": "old", "GH_REPO": "old"}
        mock_ws.return_value = tmp_path
        with patch("scripts.mq.clone_pr.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0)
            result = CliRunner().invoke(main, [
                "--repo", "https://github.com/bhatti/formicary",
                "--branch", "main",
            ])
        assert result.exit_code == 0
        mock_clone.assert_called_once()

    @patch("scripts.mq.clone_pr.clone_by_tracker")
    @patch("scripts.mq.clone_pr.load_config")
    @patch("scripts.mq.clone_pr.get_workspace_dir")
    def test_branch_flag_used_when_no_pr_number(self, mock_ws, mock_cfg, mock_clone, tmp_path):
        mock_cfg.return_value = {"GH_ORG": "org", "GH_REPO": "repo"}
        mock_ws.return_value = tmp_path
        with patch("scripts.mq.clone_pr.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(returncode=0)
            result = CliRunner().invoke(main, ["--branch", "develop"])
        assert result.exit_code == 0
        calls = mock_sub.run.call_args_list
        assert calls[0][0][0] == ["git", "fetch", "origin", "develop"]
