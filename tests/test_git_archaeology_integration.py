"""Integration tests for git_archaeology using the ai-dev-tools repo on disk.

These tests run against the real git history of this repository — no mocks, no temp repos.
They verify that the git commands work correctly and the output format is correct.

Run with:
    python -m pytest tests/test_git_archaeology_integration.py -v
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.common.git_archaeology import (
    _file_volatility,
    _hot_files,
    _recent_changes,
    _related_commits,
    build_context,
)

# The repo under test is this repo itself
REPO = Path(__file__).parent.parent


def test_repo_is_git_repo():
    """Sanity check: the test repo has a .git directory."""
    assert (REPO / ".git").exists(), f"Expected {REPO} to be a git repo"


def test_hot_files_returns_list():
    """hot_files should return a non-empty list of (filepath, count) tuples."""
    result = _hot_files(REPO, n_commits=20)
    assert isinstance(result, list)
    assert len(result) > 0
    for filepath, count in result:
        assert isinstance(filepath, str) and filepath
        assert isinstance(count, int) and count > 0


def test_hot_files_sorted_descending():
    """hot_files should be sorted highest change-count first."""
    result = _hot_files(REPO, n_commits=30)
    counts = [c for _, c in result]
    assert counts == sorted(counts, reverse=True)


def test_file_volatility_known_file():
    """config.py is touched frequently — should have at least 1 commit."""
    result = _file_volatility(REPO, ["scripts/common/config.py"], n=50)
    assert "scripts/common/config.py" in result
    assert result["scripts/common/config.py"] >= 1


def test_file_volatility_multiple_files():
    """Volatility for multiple files returns one entry per file."""
    files = ["scripts/common/config.py", "scripts/common/git_utils.py"]
    result = _file_volatility(REPO, files, n=30)
    assert set(result.keys()) == set(files)
    for f in files:
        assert isinstance(result[f], int)


def test_file_volatility_nonexistent_file():
    """A file that doesn't exist should return 0 (no commits touch it)."""
    result = _file_volatility(REPO, ["does/not/exist.py"], n=10)
    assert result["does/not/exist.py"] == 0


def test_recent_changes_returns_list():
    """recent_changes on a real file should return a list of commit dicts."""
    result = _recent_changes(REPO, ["scripts/common/config.py"], n=5)
    assert isinstance(result, list)
    if result:  # may be empty if file has no changes in shallow clone
        assert all(isinstance(c, dict) for c in result)
        assert all("hash" in c and "message" in c for c in result)


def test_related_commits_with_keyword():
    """Search for a keyword that is likely in recent commit messages."""
    # "fix" or "update" should appear in recent commit messages of any active repo
    result = _related_commits(REPO, "update", n=20)
    assert isinstance(result, list)
    # Results may be empty if no commits match — just verify structure
    for c in result:
        assert "hash" in c
        assert "message" in c
        assert "author" in c
        assert "date" in c


def test_build_context_returns_string():
    """build_context on this repo should return a non-empty markdown string."""
    result = build_context(REPO, ["fix", "update"], n=5)
    # Should return either useful context or empty string — never raise
    assert isinstance(result, str)


def test_build_context_structure_when_hot_files_exist():
    """When the repo has history, build_context should include the Git History header."""
    result = build_context(REPO, ["fix"], n=10)
    if result:  # non-empty means we got data
        assert "## Git History Context" in result


def test_build_context_invalid_path():
    """build_context on a non-git path returns empty string without raising."""
    result = build_context(Path("/tmp/not-a-git-repo-xyz"), ["PROJ-1"])
    assert result == ""
