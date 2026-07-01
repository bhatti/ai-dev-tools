"""Tests for scripts/common/claude_runner.py"""

import io
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from scripts.common.claude_runner import ClaudeResult, extract_status_json, run_claude


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
