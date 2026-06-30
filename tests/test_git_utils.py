"""Tests for scripts/common/git_utils.py"""

from unittest.mock import MagicMock, call, patch

import pytest

from scripts.common.git_utils import (
    _slug,
    commit_all,
    get_commit_count,
    make_branch_name,
)


def test_slug_basic():
    assert _slug("Add user authentication") == "add-user-authentication"


def test_slug_special_chars():
    assert _slug("Fix bug: remove #1 issue!") == "fix-bug-remove-1-issue"


def test_slug_truncates():
    long = "a" * 100
    assert len(_slug(long)) <= 40


def test_make_branch_name():
    branch = make_branch_name("42", "Add login feature", nonce="abc123")
    assert branch == "ai/42-add-login-feature-abc123"


def test_make_branch_name_long_title():
    branch = make_branch_name("42", "A" * 100, nonce="abc")
    # branch should be ai/42-{slug}-abc and slug <= 40 chars
    parts = branch.split("-")
    assert branch.startswith("ai/42-")


def test_make_branch_name_generates_nonce():
    b1 = make_branch_name("42", "title")
    b2 = make_branch_name("42", "title")
    # nonce should differ (random)
    assert b1 != b2


@patch("scripts.common.git_utils._run")
def test_commit_all_nothing_to_commit(mock_run):
    mock_run.side_effect = [
        MagicMock(returncode=0),  # git add -A
        MagicMock(stdout="", returncode=0),  # git status --porcelain (empty = nothing)
    ]
    from pathlib import Path
    result = commit_all(Path("/fake/repo"), "test commit")
    assert result is False


@patch("scripts.common.git_utils._run")
def test_commit_all_with_changes(mock_run):
    mock_run.side_effect = [
        MagicMock(returncode=0),  # git add -A
        MagicMock(stdout=" M file.py\n", returncode=0),  # git status --porcelain
        MagicMock(returncode=0),  # git commit
    ]
    from pathlib import Path
    result = commit_all(Path("/fake/repo"), "test commit")
    assert result is True


@patch("scripts.common.git_utils._run")
def test_get_commit_count_success(mock_run):
    mock_run.return_value = MagicMock(stdout="3\n", returncode=0)
    from pathlib import Path
    count = get_commit_count(Path("/fake/repo"), "main")
    assert count == 3


@patch("scripts.common.git_utils._run")
def test_get_commit_count_git_error(mock_run):
    mock_run.return_value = MagicMock(stdout="", returncode=1)
    from pathlib import Path
    count = get_commit_count(Path("/fake/repo"), "main")
    assert count == 0
