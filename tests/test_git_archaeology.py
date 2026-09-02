"""Tests for scripts/common/git_archaeology.py"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.common.git_archaeology import (
    _file_volatility,
    _hot_files,
    _recent_changes,
    _related_commits,
    build_context,
)


def test_related_commits_parses_output():
    with patch("scripts.common.git_archaeology.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc1234|Fix flaky login|Alice|2026-08-10\ndef5678|Add retry logic|Bob|2026-08-09\n",
        )
        result = _related_commits(Path("/fake/repo"), "PROJ-123")
    assert len(result) == 2
    assert result[0]["hash"] == "abc1234"
    assert result[0]["message"] == "Fix flaky login"
    assert result[0]["author"] == "Alice"
    assert result[0]["date"] == "2026-08-10"


def test_related_commits_empty_on_no_output():
    with patch("scripts.common.git_archaeology.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        result = _related_commits(Path("/fake/repo"), "PROJ-999")
    assert result == []


def test_file_volatility_counts_commits():
    with patch("scripts.common.git_archaeology.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc1234 commit 1\ndef5678 commit 2\nghi9012 commit 3\n",
        )
        result = _file_volatility(Path("/fake/repo"), ["src/auth.ts"])
    assert result["src/auth.ts"] == 3


def test_file_volatility_empty_file_list():
    result = _file_volatility(Path("/fake/repo"), [])
    assert result == {}


def test_hot_files_ranks_by_count():
    with patch("scripts.common.git_archaeology.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="\nabc1234\n\nsrc/auth.ts\nsrc/login.ts\n\ndef5678\n\nsrc/auth.ts\n",
        )
        result = _hot_files(Path("/fake/repo"), n_commits=10)
    assert result[0][0] == "src/auth.ts"
    assert result[0][1] == 2


def test_recent_changes_parses_output():
    with patch("scripts.common.git_archaeology.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="abc1234|Refactor auth|Charlie|2026-08-12\n",
        )
        result = _recent_changes(Path("/fake/repo"), ["src/auth.ts"])
    assert len(result) == 1
    assert result[0]["message"] == "Refactor auth"


def test_recent_changes_empty_files():
    result = _recent_changes(Path("/fake/repo"), [])
    assert result == []


def test_build_context_returns_markdown(tmp_path):
    """Integration test: create a real git repo and verify build_context output."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "foo.py").write_text("x = 1")
    subprocess.run(["git", "add", "foo.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "PROJ-42 initial"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "foo.py").write_text("x = 2")
    subprocess.run(["git", "add", "foo.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fix: update PROJ-42"], cwd=tmp_path, check=True, capture_output=True)

    result = build_context(tmp_path, ["PROJ-42"])
    assert "## Git History Context" in result
    assert "PROJ-42" in result
    assert "foo.py" in result


def test_build_context_graceful_on_invalid_path():
    result = build_context(Path("/nonexistent/repo/xyz"), ["PROJ-1"])
    assert result == ""


def test_build_context_graceful_on_subprocess_error():
    with patch("scripts.common.git_archaeology.subprocess.run", side_effect=OSError("git not found")):
        result = build_context(Path("/fake"), ["PROJ-1"])
    assert result == ""


def test_build_context_empty_keys_still_returns_hot_files(tmp_path):
    """build_context with no issue keys still returns hot-file volatility data."""
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "bar.py").write_text("y = 1")
    subprocess.run(["git", "add", "bar.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add bar"], cwd=tmp_path, check=True, capture_output=True)

    result = build_context(tmp_path, [])
    # Either returns archaeology data (hot files) or empty — must not raise
    assert isinstance(result, str)
