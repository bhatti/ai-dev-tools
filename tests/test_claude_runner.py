"""Tests for scripts/common/claude_runner.py"""

import io
import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.common.claude_runner import ClaudeResult, extract_status_json, run_claude, _ensure_extra_skills, _install_via_skills_cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_proc(stdout_lines: list[str], stderr_lines: list[str] | None = None, returncode: int = 0) -> MagicMock:
    """Build a minimal Popen mock that satisfies the threading drain loop."""
    mock_proc = MagicMock()
    mock_proc.stdout = iter(stdout_lines)
    # stderr must be iterable; use io.StringIO so the drain thread reads EOF cleanly
    mock_proc.stderr = io.StringIO("".join(stderr_lines or []))
    mock_proc.stdin = MagicMock()
    mock_proc.wait.return_value = None
    mock_proc.returncode = returncode
    return mock_proc


# ---------------------------------------------------------------------------
# extract_status_json
# ---------------------------------------------------------------------------

def test_extract_status_json_basic():
    output = 'some text\n{"status":"DONE","count":3}\n'
    result = extract_status_json(output)
    assert result == {"status": "DONE", "count": 3}


def test_extract_status_json_multiple_takes_last():
    output = '{"status":"PENDING"}\nsome work\n{"status":"DONE","commits":2}'
    result = extract_status_json(output)
    assert result["status"] == "DONE"
    assert result["commits"] == 2


def test_extract_status_json_none_found():
    result = extract_status_json("no json here at all")
    assert result == {}


def test_extract_status_json_nested_ignored():
    output = 'outer {"status":"DONE","files":["a","b"]} end'
    result = extract_status_json(output)
    assert result.get("status") == "DONE"


# ---------------------------------------------------------------------------
# run_claude — happy path
# ---------------------------------------------------------------------------

@patch("scripts.common.claude_runner.subprocess.Popen")
def test_run_claude_success(mock_popen, tmp_path):
    mock_popen.return_value = _make_proc(
        ['Some output\n', '{"status":"DONE","commits":2}\n']
    )
    result = run_claude("do the thing", working_dir=tmp_path, max_turns=5)
    assert result.status == "DONE"
    assert result.exit_code == 0
    # start_new_session=True is required so killpg() can kill the whole process tree
    assert mock_popen.call_args.kwargs.get("start_new_session") is True


@patch("scripts.common.claude_runner.subprocess.Popen")
def test_run_claude_blocked(mock_popen, tmp_path):
    mock_popen.return_value = _make_proc(
        ['{"status":"BLOCKED","reason":"no access"}\n']
    )
    result = run_claude("do the thing", working_dir=tmp_path)
    assert result.status == "BLOCKED"


@patch("scripts.common.claude_runner.subprocess.Popen")
def test_run_claude_writes_log_file(mock_popen, tmp_path):
    mock_popen.return_value = _make_proc(
        ['output line\n', '{"status":"DONE"}\n']
    )
    log_file = tmp_path / "test.log"
    run_claude("prompt", working_dir=tmp_path, log_file=log_file)
    assert log_file.exists()
    assert "output line" in log_file.read_text()


# ---------------------------------------------------------------------------
# run_claude — stderr handling
# ---------------------------------------------------------------------------

@patch("scripts.common.claude_runner.subprocess.Popen")
def test_run_claude_stderr_included_in_error_on_nonzero_exit(mock_popen, tmp_path):
    """Stderr content must appear in the RuntimeError raised on non-zero exit."""
    mock_popen.return_value = _make_proc(
        stdout_lines=["partial output\n"],
        stderr_lines=["Error: authentication failed\n"],
        returncode=1,
    )
    with pytest.raises(RuntimeError, match="authentication failed"):
        run_claude("prompt", working_dir=tmp_path)


@patch("scripts.common.claude_runner.subprocess.Popen")
def test_run_claude_stderr_written_to_stderr_log(mock_popen, tmp_path):
    """When stderr is non-empty, a .stderr.log file should be written next to the log."""
    mock_popen.return_value = _make_proc(
        stdout_lines=['{"status":"DONE"}\n'],
        stderr_lines=["some diagnostic\n"],
    )
    log_file = tmp_path / "plan.log"
    run_claude("prompt", working_dir=tmp_path, log_file=log_file)
    stderr_log = tmp_path / "plan.stderr.log"
    assert stderr_log.exists()
    assert "some diagnostic" in stderr_log.read_text()


@patch("scripts.common.claude_runner.subprocess.Popen")
def test_run_claude_no_stderr_log_when_stderr_empty(mock_popen, tmp_path):
    """No .stderr.log file should be created when stderr is empty."""
    mock_popen.return_value = _make_proc(
        stdout_lines=['{"status":"DONE"}\n'],
        stderr_lines=[],
    )
    log_file = tmp_path / "plan.log"
    run_claude("prompt", working_dir=tmp_path, log_file=log_file)
    assert not (tmp_path / "plan.stderr.log").exists()


@patch("scripts.common.claude_runner.subprocess.Popen")
def test_run_claude_stderr_not_in_status_json_scan(mock_popen, tmp_path):
    """Status JSON must only be extracted from stdout, not stderr."""
    # Stderr contains a JSON-like line; stdout has the real status.
    mock_popen.return_value = _make_proc(
        stdout_lines=['real output\n', '{"status":"DONE"}\n'],
        stderr_lines=['{"status":"ERROR","from":"stderr"}\n'],
    )
    result = run_claude("prompt", working_dir=tmp_path)
    assert result.status == "DONE"


@patch("scripts.common.claude_runner.subprocess.Popen")
def test_run_claude_missing_binary_returns_error_result(mock_popen, tmp_path):
    """FileNotFoundError (claude CLI not installed) must return an ERROR result, not raise."""
    mock_popen.side_effect = FileNotFoundError("claude not found")
    result = run_claude("prompt", working_dir=tmp_path)
    assert result.exit_code == 1
    assert result.status == "ERROR"


# ---------------------------------------------------------------------------
# _ensure_extra_skills — EXTRA_SKILLS_REPOS parsing and URL expansion
# ---------------------------------------------------------------------------

class TestEnsureExtraSkillsUrlExpansion:
    """Test plain-string name expansion in EXTRA_SKILLS_REPOS."""

    def _call(self, raw: str, env_overrides: dict, skills_base: Path) -> None:
        with patch.dict(os.environ, env_overrides, clear=False):
            with patch("scripts.common.claude_runner.subprocess.run") as mock_run, \
                 patch("scripts.common.claude_runner.subprocess.Popen"):
                mock_run.return_value = MagicMock(returncode=0, stderr="")
                with patch.dict(os.environ, {"EXTRA_SKILLS_REPOS": raw}, clear=False):
                    _ensure_extra_skills(skills_base)
        return mock_run

    def test_bare_name_with_bitbucket_workspace(self, tmp_path):
        env = {"BITBUCKET_WORKSPACE": "acme", "DEFAULT_TRACKER": "jira",
               "BITBUCKET_TOKEN": "tok", "BITBUCKET_USERNAME": "user@acme.io"}
        with patch.dict(os.environ, env, clear=False):
            with patch("scripts.common.claude_runner.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
                with patch.dict(os.environ, {"EXTRA_SKILLS_REPOS": "myrepo"}, clear=False):
                    _ensure_extra_skills(tmp_path)
        urls = [str(c) for call in mock_run.call_args_list for c in call.args[0]]
        assert any("bitbucket.org/acme/myrepo" in u for u in urls)

    def test_bare_name_with_github_tracker(self, tmp_path):
        env = {"GH_ORG": "myorg", "DEFAULT_TRACKER": "github", "GH_TOKEN": "ghtoken"}
        with patch.dict(os.environ, env, clear=False):
            with patch("scripts.common.claude_runner.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
                with patch.dict(os.environ, {"EXTRA_SKILLS_REPOS": "myrepo"}, clear=False):
                    _ensure_extra_skills(tmp_path)
        urls = [str(c) for call in mock_run.call_args_list for c in call.args[0]]
        assert any("github.com/myorg/myrepo" in u for u in urls)

    def test_full_url_passthrough(self, tmp_path):
        raw = json.dumps([{"url": "https://bitbucket.org/org/repo.git", "sparse": False}])
        env = {"BITBUCKET_TOKEN": "tok", "BITBUCKET_USERNAME": "usr", "DEFAULT_TRACKER": "jira",
               "EXTRA_SKILLS_REPOS": raw}
        with patch.dict(os.environ, env, clear=False):
            with patch("scripts.common.claude_runner.clone_repo") as mock_clone:
                mock_clone.return_value = tmp_path / "_extra_repo"
                _ensure_extra_skills(tmp_path)
        assert mock_clone.called
        call_url = mock_clone.call_args.args[0]
        assert "bitbucket.org/org/repo" in call_url

    def test_skills_dir_auto_detect_claude_skills(self, tmp_path):
        """Auto-detect .claude/skills directory when skills_dir not specified."""
        dest = tmp_path / "_extra_repo"
        claude_skills = dest / ".claude" / "skills"
        claude_skills.mkdir(parents=True)
        (claude_skills / "my-skill").mkdir()
        (dest / ".git").mkdir()  # mark as already-cloned so clone is skipped

        raw = json.dumps([{"url": "https://github.com/org/repo.git", "sparse": False}])
        env = {"GH_TOKEN": "tok", "DEFAULT_TRACKER": "github", "EXTRA_SKILLS_REPOS": raw}
        with patch.dict(os.environ, env, clear=False):
            _ensure_extra_skills(tmp_path)
        assert (tmp_path / "my-skill").is_symlink()

    def test_skills_dir_auto_detect_skills(self, tmp_path):
        """Fall back to 'skills' directory when .claude/skills absent."""
        dest = tmp_path / "_extra_repo"
        skills_dir = dest / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "my-skill").mkdir()
        (dest / ".git").mkdir()  # mark as already-cloned

        raw = json.dumps([{"url": "https://github.com/org/repo.git", "sparse": False}])
        env = {"GH_TOKEN": "tok", "DEFAULT_TRACKER": "github", "EXTRA_SKILLS_REPOS": raw}
        with patch.dict(os.environ, env, clear=False):
            _ensure_extra_skills(tmp_path)
        assert (tmp_path / "my-skill").is_symlink()

    def test_invalid_json_logs_warning(self, tmp_path, capsys):
        with patch.dict(os.environ, {"EXTRA_SKILLS_REPOS": "not-json{", "BITBUCKET_WORKSPACE": "ws",
                                      "DEFAULT_TRACKER": "jira"}, clear=False):
            with patch("scripts.common.claude_runner.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="")
                _ensure_extra_skills(tmp_path)
        # The bare string "not-json{" is not a valid URL/name — should warn but not crash
        # (it tries to expand as a bitbucket URL then fails gracefully)

    def test_empty_value_is_noop(self, tmp_path):
        with patch.dict(os.environ, {"EXTRA_SKILLS_REPOS": ""}, clear=False):
            with patch("scripts.common.claude_runner.subprocess.run") as mock_run:
                _ensure_extra_skills(tmp_path)
        mock_run.assert_not_called()

    def test_bitbucket_auto_credentials_injected(self, tmp_path):
        """BITBUCKET_USERNAME + BITBUCKET_TOKEN auto-injected into clone call."""
        raw = json.dumps([{"url": "https://bitbucket.org/org/repo.git", "sparse": False}])
        env = {"BITBUCKET_TOKEN": "secret", "BITBUCKET_USERNAME": "user@example.com",
               "DEFAULT_TRACKER": "jira", "EXTRA_SKILLS_REPOS": raw}
        with patch.dict(os.environ, env, clear=False):
            with patch("scripts.common.claude_runner.clone_repo") as mock_clone:
                mock_clone.return_value = tmp_path / "_extra_repo"
                _ensure_extra_skills(tmp_path)
        assert mock_clone.called
        kwargs = mock_clone.call_args.kwargs
        assert kwargs.get("http_token") == "secret"
        assert kwargs.get("http_username") == "user@example.com"

    def test_comma_separated_plain_urls(self, tmp_path):
        """Comma-separated list of plain URLs must expand to two repos."""
        raw = "https://github.com/org/repo1.git,https://github.com/org/repo2.git"
        env = {"GH_TOKEN": "tok", "DEFAULT_TRACKER": "github", "EXTRA_SKILLS_REPOS": raw}
        with patch.dict(os.environ, env, clear=False):
            with patch("scripts.common.claude_runner.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
                _ensure_extra_skills(tmp_path)
        # Each sparse clone invokes subprocess.run at least once
        urls = [str(c) for call in mock_run.call_args_list for c in call.args[0]]
        assert any("repo1" in u for u in urls)
        assert any("repo2" in u for u in urls)

    def test_skills_cli_prefix_routes_to_skills_cli(self, tmp_path, capsys):
        """'skills-cli:org/repo' prefix must route to _install_via_skills_cli."""
        raw = "skills-cli:nutlope/hallmark"
        env = {"GH_TOKEN": "tok", "DEFAULT_TRACKER": "github", "EXTRA_SKILLS_REPOS": raw}
        with patch.dict(os.environ, env, clear=False):
            with patch("scripts.common.claude_runner.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
                _ensure_extra_skills(tmp_path)
        # skills-cli path calls npx
        cmds = [call.args[0] for call in mock_run.call_args_list]
        assert any("npx" in cmd for cmd in cmds), "Expected npx call for skills-cli prefix"

    def test_skills_cli_prefix_with_full_url(self, tmp_path):
        """'skills-cli:https://...' must route to skills-cli, not sparse clone."""
        raw = "skills-cli:https://github.com/nutlope/hallmark"
        env = {"GH_TOKEN": "tok", "DEFAULT_TRACKER": "github", "EXTRA_SKILLS_REPOS": raw}
        with patch.dict(os.environ, env, clear=False):
            with patch("scripts.common.claude_runner.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
                _ensure_extra_skills(tmp_path)
        cmds = [call.args[0] for call in mock_run.call_args_list]
        assert any("npx" in cmd for cmd in cmds)


# ---------------------------------------------------------------------------
# _install_via_skills_cli
# ---------------------------------------------------------------------------

class TestInstallViaSkillsCli:
    """Test the skills-cli (vercel-labs/skills) installation path."""

    def test_passes_correct_flags_no_target(self, tmp_path):
        """Must pass --agent and --yes; must NOT pass --target (invalid flag)."""
        with patch("scripts.common.claude_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            _install_via_skills_cli("org/skills-repo", tmp_path)
        assert mock_run.called
        cmd = mock_run.call_args.args[0]
        assert "npx" in cmd
        assert "skills" in cmd
        assert "add" in cmd
        assert "org/skills-repo" in cmd
        assert "--agent" in cmd
        assert "claude-code" in cmd
        assert "--yes" in cmd
        assert "--target" not in cmd

    def test_forwards_gh_token_env(self, tmp_path):
        """GH_TOKEN must be forwarded in the subprocess environment."""
        with patch("scripts.common.claude_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            with patch.dict(os.environ, {"GH_TOKEN": "mytoken"}, clear=False):
                _install_via_skills_cli("org/repo", tmp_path)
        env = mock_run.call_args.kwargs.get("env", {})
        assert env.get("GH_TOKEN") == "mytoken"
        assert env.get("GITHUB_TOKEN") == "mytoken"

    def test_nonzero_exit_logs_warning(self, tmp_path, capsys):
        """A non-zero exit from npx must print a WARNING, not raise."""
        with patch("scripts.common.claude_runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="fatal: auth failed", stdout="")
            _install_via_skills_cli("org/repo", tmp_path)  # must not raise
        captured = capsys.readouterr()
        assert "WARNING" in captured.err

    def test_npx_not_found_logs_warning(self, tmp_path, capsys):
        """FileNotFoundError (npx not installed) must log a warning, not raise."""
        with patch("scripts.common.claude_runner.subprocess.run", side_effect=FileNotFoundError):
            _install_via_skills_cli("org/repo", tmp_path)  # must not raise
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "npx" in captured.err
