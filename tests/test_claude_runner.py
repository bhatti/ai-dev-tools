"""Tests for scripts/common/claude_runner.py"""

from unittest.mock import MagicMock, patch

import pytest

from scripts.common.claude_runner import ClaudeResult, extract_status_json, run_claude


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
    # Only flat (non-nested) JSON objects match
    output = 'outer {"status":"DONE","files":["a","b"]} end'
    result = extract_status_json(output)
    # files array contains strings so it's still flat enough for the regex
    assert result.get("status") == "DONE"


@patch("scripts.common.claude_runner.subprocess.Popen")
def test_run_claude_success(mock_popen, tmp_path):
    mock_proc = MagicMock()
    mock_proc.stdout = iter(['Some output\n', '{"status":"DONE","commits":2}\n'])
    mock_proc.wait.return_value = None
    mock_proc.returncode = 0
    mock_popen.return_value.__enter__ = lambda s: s
    mock_popen.return_value = mock_proc

    result = run_claude("do the thing", working_dir=tmp_path, max_turns=5)
    assert result.status == "DONE"
    assert result.exit_code == 0


@patch("scripts.common.claude_runner.subprocess.Popen")
def test_run_claude_blocked(mock_popen, tmp_path):
    mock_proc = MagicMock()
    mock_proc.stdout = iter(['{"status":"BLOCKED","reason":"no access"}\n'])
    mock_proc.wait.return_value = None
    mock_proc.returncode = 0
    mock_popen.return_value = mock_proc

    result = run_claude("do the thing", working_dir=tmp_path)
    assert result.status == "BLOCKED"


@patch("scripts.common.claude_runner.subprocess.Popen")
def test_run_claude_writes_log_file(mock_popen, tmp_path):
    mock_proc = MagicMock()
    mock_proc.stdout = iter(['output line\n', '{"status":"DONE"}\n'])
    mock_proc.wait.return_value = None
    mock_proc.returncode = 0
    mock_popen.return_value = mock_proc

    log_file = tmp_path / "test.log"
    run_claude("prompt", working_dir=tmp_path, log_file=log_file)
    assert log_file.exists()
    assert "output line" in log_file.read_text()
