"""Tests for check_pr_state and BitbucketNetworkError handling."""
from unittest.mock import MagicMock, patch

import pytest

from scripts.common.bitbucket_api import BitbucketNetworkError


class TestBitbucketNetworkError:
    """BitbucketNetworkError raised on timeout/connection failure; callers can pause."""

    def test_raised_on_timeout(self):
        import requests
        from scripts.common.bitbucket_api import get_pr
        with patch("scripts.common.bitbucket_api.requests.get",
                   side_effect=requests.exceptions.Timeout("timed out")):
            with pytest.raises(BitbucketNetworkError, match="timed out"):
                get_pr({"BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}, "ws", "repo", 42)

    def test_raised_on_connection_error(self):
        import requests
        from scripts.common.bitbucket_api import get_pr
        with patch("scripts.common.bitbucket_api.requests.get",
                   side_effect=requests.exceptions.ConnectionError("refused")):
            with pytest.raises(BitbucketNetworkError, match="connection failed"):
                get_pr({"BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}, "ws", "repo", 42)

    def test_not_raised_on_404(self):
        from scripts.common.bitbucket_api import get_pr
        mock_resp = MagicMock()
        mock_resp.ok = False
        with patch("scripts.common.bitbucket_api.requests.get", return_value=mock_resp):
            result = get_pr({"BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}, "ws", "repo", 99)
        assert result is None

    def test_is_subclass_of_ioerror(self):
        # Ensures callers using `except IOError` catch it
        err = BitbucketNetworkError("test")
        assert isinstance(err, IOError)


class TestCheckPrStateExitCodes:
    """check_pr_state exits 3 on transient network error (PAUSE) not 1 (fail)."""

    def _config(self):
        return {
            "BITBUCKET_USERNAME": "user", "BITBUCKET_TOKEN": "tok",
            "BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "repo",
            "WORKSPACE_DIR": "/tmp",
        }

    def test_exits_3_on_network_timeout(self, tmp_path):
        from click.testing import CliRunner
        from scripts.jira.check_pr_state import main

        pr_json = tmp_path / "pr-audit" / "pr.json"
        pr_json.parent.mkdir(parents=True)
        import json
        pr_json.write_text(json.dumps({
            "number": 42, "workspace": "ws", "repo": "repo",
            "url": "https://bitbucket.org/ws/repo/pull-requests/42",
        }))

        with (
            patch("scripts.jira.check_pr_state.load_config", return_value=self._config()),
            patch("scripts.jira.check_pr_state.read_json",
                  return_value={"number": 42, "workspace": "ws", "repo": "repo"}),
            patch("scripts.jira.check_pr_state.write_json"),
            patch("scripts.jira.check_pr_state.get_pr_state",
                  side_effect=BitbucketNetworkError("timed out")),
        ):
            runner = CliRunner()
            result = runner.invoke(main, ["--issue-id", "pr-audit"])

        assert result.exit_code == 3

    def test_exits_1_on_unknown_state(self):
        from click.testing import CliRunner
        from scripts.jira.check_pr_state import main

        with (
            patch("scripts.jira.check_pr_state.load_config", return_value=self._config()),
            patch("scripts.jira.check_pr_state.read_json",
                  return_value={"number": 99, "workspace": "ws", "repo": "repo"}),
            patch("scripts.jira.check_pr_state.write_json"),
            patch("scripts.jira.check_pr_state.get_pr_state", return_value="UNKNOWN"),
        ):
            runner = CliRunner()
            result = runner.invoke(main, ["--issue-id", "pr-audit"])

        assert result.exit_code == 1


class TestGHCheckPrStateExitCodes:
    """gh/check_pr_state exits 3 on CLI error (PAUSE) not 1 (fail)."""

    def _config(self):
        return {"GH_ORG": "org", "GH_REPO": "repo", "GH_TOKEN": "tok", "WORKSPACE_DIR": "/tmp"}

    def test_exits_3_on_gh_cli_error(self):
        from click.testing import CliRunner
        from scripts.gh.check_pr_state import main
        from unittest.mock import MagicMock

        pr_data = {"number": 42, "url": "https://github.com/org/repo/pull/42"}
        mock_run_result = MagicMock()
        mock_run_result.returncode = 1
        mock_run_result.stderr = "read tcp: i/o timeout"

        with (
            patch("scripts.gh.check_pr_state.load_config", return_value=self._config()),
            patch("scripts.gh.check_pr_state.read_json", return_value=pr_data),
            patch("scripts.gh.check_pr_state.write_json"),
            patch("scripts.gh.check_pr_state._run", return_value=mock_run_result),
        ):
            runner = CliRunner()
            result = runner.invoke(main, ["--issue-id", "42"])

        assert result.exit_code == 3

    def test_exits_0_on_merged(self):
        from click.testing import CliRunner
        from scripts.gh.check_pr_state import main
        from unittest.mock import MagicMock
        import json

        pr_data = {"number": 5, "url": "https://github.com/org/repo/pull/5"}
        mock_run_result = MagicMock()
        mock_run_result.returncode = 0
        mock_run_result.stdout = json.dumps({"state": "OPEN", "mergedAt": "2026-09-30T10:00:00Z"})

        with (
            patch("scripts.gh.check_pr_state.load_config", return_value=self._config()),
            patch("scripts.gh.check_pr_state.read_json", return_value=pr_data),
            patch("scripts.gh.check_pr_state.write_json"),
            patch("scripts.gh.check_pr_state._run", return_value=mock_run_result),
            patch("scripts.gh.check_pr_state._call_learn"),
            patch("scripts.gh.check_pr_state.notify"),
        ):
            runner = CliRunner()
            result = runner.invoke(main, ["--issue-id", "5"])

        assert result.exit_code == 0
