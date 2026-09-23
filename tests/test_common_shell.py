"""Tests for scripts/common/shell.py — run_cmd helper."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from scripts.common.shell import run_cmd


class TestRunCmd:
    def test_success_returns_result(self):
        result = run_cmd(["echo", "hello"])
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_check_raises_on_nonzero(self):
        with pytest.raises(subprocess.CalledProcessError):
            run_cmd(["false"])

    def test_check_false_does_not_raise(self):
        result = run_cmd(["false"], check=False)
        assert result.returncode != 0

    def test_cwd_changes_working_directory(self, tmp_path):
        # Running pwd in tmp_path must return tmp_path, not cwd of the test process.
        result = run_cmd(["pwd"], cwd=str(tmp_path))
        assert result.stdout.strip() == str(tmp_path)

    def test_cwd_none_uses_caller_cwd(self):
        # Default (no cwd) must not raise — just runs in the current directory.
        result = run_cmd(["pwd"])
        assert result.returncode == 0
        assert len(result.stdout.strip()) > 0

    @patch("scripts.common.shell.subprocess.run")
    def test_cwd_forwarded_to_subprocess(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        run_cmd(["git", "status"], cwd="/some/dir")
        _, kwargs = mock_run.call_args
        assert kwargs["cwd"] == "/some/dir"

    def test_stderr_in_exception_message(self):
        # When the command fails, CalledProcessError.stderr must be populated.
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            run_cmd(["ls", "/no-such-path-xyz-abc"])
        assert exc_info.value.stderr or exc_info.value.output
