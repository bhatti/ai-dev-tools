"""Tests for scripts/common/git_utils.py"""

from unittest.mock import MagicMock, call, patch

import pytest

from scripts.common.git_utils import (
    _slug,
    commit_all,
    create_branch,
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


@patch("scripts.common.git_utils._run")
def test_get_commit_count_uses_origin_ref_first(mock_run):
    """Uses origin/<base> first so shallow clones without local branch work."""
    # First call: origin/stage ref succeeds
    mock_run.return_value = MagicMock(stdout="5\n", returncode=0)
    from pathlib import Path
    count = get_commit_count(Path("/fake/repo"), "stage")
    assert count == 5
    # Should have tried origin/stage first
    first_call_cmd = mock_run.call_args_list[0][0][0]
    assert any("origin/stage..HEAD" in arg for arg in first_call_cmd)


@patch("scripts.common.git_utils._run")
def test_get_commit_count_falls_back_to_local_branch(mock_run):
    """Falls back to bare branch name when origin/ ref fails."""
    mock_run.side_effect = [
        MagicMock(stdout="", returncode=1),   # origin/stage fails
        MagicMock(stdout="3\n", returncode=0), # bare 'stage' succeeds
    ]
    from pathlib import Path
    count = get_commit_count(Path("/fake/repo"), "stage")
    assert count == 3


# ── create_branch tests ───────────────────────────────────────────────────────

@patch("scripts.common.git_utils._run")
def test_create_branch_already_local(mock_run):
    """Reuses an existing local branch without touching origin."""
    mock_run.return_value = MagicMock(stdout="  ai/42-my-branch-abc\n", returncode=0)
    from pathlib import Path
    result = create_branch(Path("/repo"), "ai/42-my-branch-abc")
    assert result == "ai/42-my-branch-abc"
    # First call: git branch --list; second: git checkout
    assert mock_run.call_count == 2
    assert mock_run.call_args_list[1][0][0] == ["git", "checkout", "ai/42-my-branch-abc"]


@patch("scripts.common.git_utils._run")
def test_create_branch_new_no_base(mock_run):
    """Creates a new branch from current HEAD when no base_branch given."""
    mock_run.side_effect = [
        MagicMock(stdout="", returncode=0),          # branch --list (empty = not local)
        MagicMock(stdout="", returncode=1),           # rev-parse tracking ref (absent)
        MagicMock(stdout="", returncode=0),           # ls-remote (empty = not on remote)
        MagicMock(returncode=0),                      # checkout -b
    ]
    from pathlib import Path
    result = create_branch(Path("/repo"), "ai/42-my-branch-abc")
    assert result == "ai/42-my-branch-abc"
    last_call = mock_run.call_args_list[-1][0][0]
    assert last_call == ["git", "checkout", "-b", "ai/42-my-branch-abc"]


@patch("scripts.common.git_utils._run")
def test_create_branch_new_with_base_already_fetched(mock_run):
    """Forks new branch from origin/<base_branch> when tracking ref already exists."""
    mock_run.side_effect = [
        MagicMock(stdout="", returncode=0),          # branch --list (not local)
        MagicMock(stdout="", returncode=1),           # rev-parse feature branch tracking (absent)
        MagicMock(stdout="", returncode=0),           # ls-remote feature branch (not on remote)
        MagicMock(stdout="abc123\n", returncode=0),  # rev-parse origin/stage (exists)
        MagicMock(returncode=0),                      # checkout -b from origin/stage
    ]
    from pathlib import Path
    result = create_branch(Path("/repo"), "ai/42-my-branch-abc", base_branch="stage")
    assert result == "ai/42-my-branch-abc"
    last_call = mock_run.call_args_list[-1][0][0]
    assert last_call == ["git", "checkout", "-b", "ai/42-my-branch-abc", "origin/stage"]


@patch("scripts.common.git_utils._run")
def test_create_branch_new_with_base_needs_fetch(mock_run):
    """Fetches origin/<base_branch> first when tracking ref is absent, then forks."""
    mock_run.side_effect = [
        MagicMock(stdout="", returncode=0),          # branch --list (not local)
        MagicMock(stdout="", returncode=1),           # rev-parse feature branch tracking (absent)
        MagicMock(stdout="", returncode=0),           # ls-remote feature branch (not on remote)
        MagicMock(stdout="", returncode=1),           # rev-parse origin/stage (absent)
        MagicMock(returncode=0),                      # fetch origin stage
        MagicMock(returncode=0),                      # checkout -b from origin/stage
    ]
    from pathlib import Path
    result = create_branch(Path("/repo"), "ai/42-my-branch-abc", base_branch="stage")
    assert result == "ai/42-my-branch-abc"
    # fetch call should include the stage refspec
    fetch_call = mock_run.call_args_list[-2][0][0]
    assert "fetch" in fetch_call
    assert "+refs/heads/stage:refs/remotes/origin/stage" in fetch_call
    # final checkout should reference origin/stage
    checkout_call = mock_run.call_args_list[-1][0][0]
    assert checkout_call == ["git", "checkout", "-b", "ai/42-my-branch-abc", "origin/stage"]
