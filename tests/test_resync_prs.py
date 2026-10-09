# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for scripts/resync/*.py — no real git operations."""
from __future__ import annotations

import subprocess
from dataclasses import fields
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from scripts.resync.pr_syncer import (
    SyncResult,
    _changed_files_from_diff,
    _count_diff_lines,
    _verify_diff,
    sync_pr,
)
from scripts.resync.run_resync_prs import (
    _fetch_target_prs,
    _normalize_gh_pr,
    _normalize_bb_pr,
    _parse_slack_flags,
)


# ---------------------------------------------------------------------------
# _parse_slack_flags
# ---------------------------------------------------------------------------

def test_parse_slack_flags_dry_run():
    flags = _parse_slack_flags("resync-prs --dry-run")
    assert flags["dry_run"] is True


def test_parse_slack_flags_dry_run_alt_spelling():
    flags = _parse_slack_flags("resync prs --dryrun")
    assert flags["dry_run"] is True


def test_parse_slack_flags_tracker():
    flags = _parse_slack_flags("resync-prs --tracker jira")
    assert flags["tracker"] == "jira"


def test_parse_slack_flags_no_flags():
    flags = _parse_slack_flags("resync-prs")
    assert flags["dry_run"] is False
    assert flags["me"] is False
    assert flags["tracker"] == ""
    assert flags["pr_urls"] == []
    assert flags["pr_numbers"] == []


def test_parse_slack_flags_me():
    flags = _parse_slack_flags("resync-prs --me")
    assert flags["me"] is True
    assert flags["dry_run"] is False


def test_parse_slack_flags_me_with_dry_run():
    flags = _parse_slack_flags("resync-prs --me --dry-run")
    assert flags["me"] is True
    assert flags["dry_run"] is True


def test_parse_slack_flags_me_not_set_by_default():
    flags = _parse_slack_flags("resync-prs --dry-run --tracker github")
    assert flags["me"] is False


def test_parse_slack_flags_github_pr_url():
    msg = "resync-prs https://github.com/org/repo/pull/42"
    flags = _parse_slack_flags(msg)
    assert "https://github.com/org/repo/pull/42" in flags["pr_urls"]


def test_parse_slack_flags_bitbucket_pr_url():
    msg = "resync-prs https://bitbucket.org/ws/repo/pull-requests/77"
    flags = _parse_slack_flags(msg)
    assert "https://bitbucket.org/ws/repo/pull-requests/77" in flags["pr_urls"]


def test_parse_slack_flags_bare_numbers_with_hash():
    # #N form works for any size PR number
    flags = _parse_slack_flags("resync-prs #42 #101")
    assert 42 in flags["pr_numbers"]
    assert 101 in flags["pr_numbers"]


def test_parse_slack_flags_bare_small_numbers_without_hash_ignored():
    # Small numbers (< 10000) without # are rejected — too risky for git ops
    # e.g. "resync-prs fix the 100 failing tests" must NOT trigger PR #100
    flags = _parse_slack_flags("resync-prs 42 101")
    assert 42 not in flags["pr_numbers"]
    assert 101 not in flags["pr_numbers"]


def test_parse_slack_flags_bare_large_numbers_accepted():
    # Large PR numbers (>= 10000) without # are accepted — unambiguously PR refs
    flags = _parse_slack_flags("resync-prs 48239 48240 --dry-run")
    assert 48239 in flags["pr_numbers"]
    assert 48240 in flags["pr_numbers"]
    assert flags["dry_run"] is True


def test_parse_slack_flags_bare_large_number_with_url():
    # Mix of URL and bare large number — both should be captured
    flags = _parse_slack_flags(
        "resync-prs https://bitbucket.org/ws/repo/pull-requests/48239 49866"
    )
    assert "https://bitbucket.org/ws/repo/pull-requests/48239" in flags["pr_urls"]
    # 49866 is a standalone large number in the message (not part of URL)
    assert 49866 in flags["pr_numbers"]
    # 48239 must not be double-counted (already in pr_urls)
    assert 48239 not in flags["pr_numbers"]


def test_parse_slack_flags_bb_url_with_overview_suffix():
    # Bitbucket URLs often end in /overview — must still be parsed correctly
    msg = "https://bitbucket.org/org/repo/pull-requests/48239/overview --dry-run"
    flags = _parse_slack_flags(msg)
    assert any("48239" in u for u in flags["pr_urls"])
    assert flags["dry_run"] is True


def test_parse_slack_flags_combined():
    flags = _parse_slack_flags(
        "resync-prs https://github.com/org/repo/pull/10 --dry-run --tracker github"
    )
    assert flags["dry_run"] is True
    assert flags["tracker"] == "github"
    assert len(flags["pr_urls"]) == 1


# ---------------------------------------------------------------------------
# Diff utilities
# ---------------------------------------------------------------------------

_SAMPLE_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,3 +1,4 @@
 line1
+added line
 line2
-removed line
diff --git a/bar.py b/bar.py
--- a/bar.py
+++ b/bar.py
@@ -5,2 +5,2 @@
-old
+new
"""


def test_count_diff_lines():
    count = _count_diff_lines(_SAMPLE_DIFF)
    assert count == 4  # +added, -removed, -old, +new


def test_changed_files_from_diff():
    files = _changed_files_from_diff(_SAMPLE_DIFF)
    assert files == {"foo.py", "bar.py"}


def test_verify_diff_identical():
    ok, reason = _verify_diff(_SAMPLE_DIFF, _SAMPLE_DIFF)
    assert ok is True
    assert reason == ""


def test_verify_diff_within_threshold():
    # Add one extra line (4→5 = 25% change) — exceeds 5% default
    extra_line = "+extra\n"
    modified = _SAMPLE_DIFF + extra_line
    ok, reason = _verify_diff(_SAMPLE_DIFF, modified, threshold=0.3)
    assert ok is True  # within 30% threshold


def test_verify_diff_major_drift():
    # 30% shrink with 5% threshold → should fail
    shorter = _SAMPLE_DIFF.replace("+added line\n", "").replace("-removed line\n", "")
    ok, reason = _verify_diff(_SAMPLE_DIFF, shorter, threshold=0.05)
    assert ok is False
    assert "threshold" in reason


def test_verify_diff_different_files():
    extra_file_diff = _SAMPLE_DIFF + "diff --git a/new.py b/new.py\n--- a/new.py\n+++ b/new.py\n+new file\n"
    ok, reason = _verify_diff(_SAMPLE_DIFF, extra_file_diff)
    assert ok is False
    assert "new.py" in reason


def test_verify_diff_empty_before_and_after():
    ok, _ = _verify_diff("", "")
    assert ok is True


# ---------------------------------------------------------------------------
# Author guard
# ---------------------------------------------------------------------------

_PR_OWN = {
    "number": 1, "title": "My PR", "url": "https://github.com/org/r/pull/1",
    "author": "alice", "headRefName": "feat/x", "baseRefName": "main",
}
_PR_OTHER = {
    "number": 2, "title": "Their PR", "url": "https://github.com/org/r/pull/2",
    "author": "bob", "headRefName": "feat/y", "baseRefName": "main",
}

_BASE_CONFIG = {
    "GH_ORG": "org", "GH_REPO": "repo", "GH_TOKEN": "ghp_test",
    "WORKSPACE_DIR": "/tmp",
}


@patch("scripts.resync.pr_syncer.resolve_clone_auth", return_value=("token", "x-access-token", ""))
@patch("scripts.resync.pr_syncer._build_clone_url", return_value="https://github.com/org/repo.git")
@patch("scripts.resync.pr_syncer.clone_repo")
@patch("scripts.resync.pr_syncer._fetch_branch")
@patch("scripts.resync.pr_syncer._ensure_merge_base", return_value=True)
@patch("scripts.resync.pr_syncer._diff_snapshot", return_value=_SAMPLE_DIFF)
@patch("scripts.resync.pr_syncer._count_new_commits", return_value=0)
def test_author_guard_up_to_date_own_pr(
    mock_commits, mock_diff, mock_merge_base, mock_fetch, mock_clone, mock_url, mock_auth, tmp_path
):
    r = sync_pr(_PR_OWN, _BASE_CONFIG, dry_run=True, current_user="alice",
                tracker="github", author_guard=True, work_dir=tmp_path)
    assert r.status == "up_to_date"


@patch("scripts.resync.pr_syncer.resolve_clone_auth", return_value=("token", "x-access-token", ""))
@patch("scripts.resync.pr_syncer._build_clone_url", return_value="https://github.com/org/repo.git")
def test_author_guard_skips_other_pr(mock_url, mock_auth, tmp_path):
    r = sync_pr(_PR_OTHER, _BASE_CONFIG, dry_run=True, current_user="alice",
                tracker="github", author_guard=True, work_dir=tmp_path)
    assert r.status == "skipped"
    assert "alice" in (r.error or "")
    assert "bob" in (r.error or "")


@patch("scripts.resync.pr_syncer.resolve_clone_auth", return_value=("token", "x-access-token", ""))
@patch("scripts.resync.pr_syncer._build_clone_url", return_value="https://github.com/org/repo.git")
def test_author_guard_empty_user_returns_error(mock_url, mock_auth, tmp_path):
    # author_guard=True but current_user="" — must error rather than silently process
    r = sync_pr(_PR_OWN, _BASE_CONFIG, dry_run=True, current_user="",
                tracker="github", author_guard=True, work_dir=tmp_path)
    assert r.status == "error"
    assert r.error is not None
    assert "current user" in (r.error or "").lower() or "identity" in (r.error or "").lower()


@patch("scripts.resync.pr_syncer.resolve_clone_auth", return_value=("token", "x-access-token", ""))
@patch("scripts.resync.pr_syncer._build_clone_url", return_value="https://github.com/org/repo.git")
@patch("scripts.resync.pr_syncer.clone_repo")
@patch("scripts.resync.pr_syncer._fetch_branch")
@patch("scripts.resync.pr_syncer._ensure_merge_base", return_value=True)
@patch("scripts.resync.pr_syncer._diff_snapshot", return_value=_SAMPLE_DIFF)
@patch("scripts.resync.pr_syncer._count_new_commits", return_value=0)
def test_author_guard_explicit_bypasses_check(
    mock_commits, mock_diff, mock_merge_base, mock_fetch, mock_clone, mock_url, mock_auth, tmp_path
):
    # explicit_mode=True → author_guard=False → should process even if author != current_user
    r = sync_pr(_PR_OTHER, _BASE_CONFIG, dry_run=True, current_user="alice",
                tracker="github", author_guard=False, work_dir=tmp_path)
    assert r.status == "up_to_date"  # 0 new commits → up_to_date, not skipped


# ---------------------------------------------------------------------------
# Up-to-date (no new commits on base)
# ---------------------------------------------------------------------------

@patch("scripts.resync.pr_syncer.resolve_clone_auth", return_value=("token", "x-access-token", ""))
@patch("scripts.resync.pr_syncer._build_clone_url", return_value="https://github.com/org/repo.git")
@patch("scripts.resync.pr_syncer.clone_repo")
@patch("scripts.resync.pr_syncer._fetch_branch")
@patch("scripts.resync.pr_syncer._ensure_merge_base", return_value=True)
@patch("scripts.resync.pr_syncer._diff_snapshot", return_value=_SAMPLE_DIFF)
@patch("scripts.resync.pr_syncer._count_new_commits", return_value=0)
def test_up_to_date_no_push(
    mock_commits, mock_diff, mock_merge_base, mock_fetch, mock_clone, mock_url, mock_auth, tmp_path
):
    with patch("scripts.resync.pr_syncer.push_branch") as mock_push:
        r = sync_pr(_PR_OWN, _BASE_CONFIG, dry_run=False, current_user="alice",
                    tracker="github", author_guard=False, work_dir=tmp_path)
    assert r.status == "up_to_date"
    mock_push.assert_not_called()


# ---------------------------------------------------------------------------
# Conflict detection
# ---------------------------------------------------------------------------

def _make_merge_fail(*args, **kwargs):
    # simulate 'git merge' returning non-zero
    proc = MagicMock()
    proc.returncode = 1
    proc.stdout = "CONFLICT (content): Merge conflict in foo.py"
    proc.stderr = ""
    return proc


@patch("scripts.resync.pr_syncer.resolve_clone_auth", return_value=("token", "x-access-token", ""))
@patch("scripts.resync.pr_syncer._build_clone_url", return_value="https://github.com/org/repo.git")
@patch("scripts.resync.pr_syncer.clone_repo")
@patch("scripts.resync.pr_syncer._fetch_branch")
@patch("scripts.resync.pr_syncer._diff_snapshot", return_value=_SAMPLE_DIFF)
@patch("scripts.resync.pr_syncer._count_new_commits", return_value=3)
@patch("scripts.resync.pr_syncer.configure_git")
@patch("scripts.resync.pr_syncer.create_branch")
@patch("scripts.resync.pr_syncer._get_conflict_files", return_value=["foo.py"])
@patch("scripts.resync.pr_syncer._run_git")
def test_conflict_aborts_merge(
    mock_run_git, mock_conflict_files, mock_create, mock_configure,
    mock_commits, mock_diff, mock_fetch, mock_clone, mock_url, mock_auth, tmp_path,
):
    # merge returns non-zero
    def side_effect(cmd, cwd, check=True):
        if "merge" in cmd and "--no-ff" in cmd:
            proc = MagicMock()
            proc.returncode = 1
            proc.stdout = "CONFLICT"
            proc.stderr = ""
            return proc
        # git merge --abort
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    mock_run_git.side_effect = side_effect

    r = sync_pr(_PR_OWN, _BASE_CONFIG, dry_run=False, current_user="alice",
                tracker="github", author_guard=False, work_dir=tmp_path)
    assert r.status == "conflict"
    assert "foo.py" in r.conflict_files

    # Verify git merge --abort was called
    abort_calls = [c for c in mock_run_git.call_args_list if "--abort" in c[0][0]]
    assert abort_calls, "git merge --abort should have been called"


# ---------------------------------------------------------------------------
# Dry-run does not push
# ---------------------------------------------------------------------------

@patch("scripts.resync.pr_syncer.resolve_clone_auth", return_value=("token", "x-access-token", ""))
@patch("scripts.resync.pr_syncer._build_clone_url", return_value="https://github.com/org/repo.git")
@patch("scripts.resync.pr_syncer.clone_repo")
@patch("scripts.resync.pr_syncer._fetch_branch")
@patch("scripts.resync.pr_syncer._diff_snapshot", return_value=_SAMPLE_DIFF)
@patch("scripts.resync.pr_syncer._count_new_commits", return_value=2)
@patch("scripts.resync.pr_syncer.configure_git")
@patch("scripts.resync.pr_syncer.create_branch")
@patch("scripts.resync.pr_syncer._has_conflict_markers", return_value=False)
@patch("scripts.resync.pr_syncer._verify_diff", return_value=(True, ""))
@patch("scripts.resync.pr_syncer._run_git")
def test_dry_run_no_push(
    mock_run_git, mock_verify, mock_markers, mock_create, mock_configure,
    mock_commits, mock_diff, mock_fetch, mock_clone, mock_url, mock_auth, tmp_path,
):
    def side_effect(cmd, cwd, check=True):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "abc1234\n"
        proc.stderr = ""
        return proc

    mock_run_git.side_effect = side_effect

    with patch("scripts.resync.pr_syncer.push_branch") as mock_push:
        r = sync_pr(_PR_OWN, _BASE_CONFIG, dry_run=True, current_user="alice",
                    tracker="github", author_guard=False, work_dir=tmp_path)

    assert r.status == "synced"
    assert r.dry_run is True
    mock_push.assert_not_called()


# ---------------------------------------------------------------------------
# Diff integrity failure aborts push
# ---------------------------------------------------------------------------

@patch("scripts.resync.pr_syncer.resolve_clone_auth", return_value=("token", "x-access-token", ""))
@patch("scripts.resync.pr_syncer._build_clone_url", return_value="https://github.com/org/repo.git")
@patch("scripts.resync.pr_syncer.clone_repo")
@patch("scripts.resync.pr_syncer._fetch_branch")
@patch("scripts.resync.pr_syncer._diff_snapshot", return_value=_SAMPLE_DIFF)
@patch("scripts.resync.pr_syncer._count_new_commits", return_value=2)
@patch("scripts.resync.pr_syncer.configure_git")
@patch("scripts.resync.pr_syncer.create_branch")
@patch("scripts.resync.pr_syncer._verify_diff", return_value=(False, "unexpected new files: ['secret.py']"))
@patch("scripts.resync.pr_syncer._run_git")
def test_diff_integrity_failure_no_push(
    mock_run_git, mock_verify, mock_create, mock_configure,
    mock_commits, mock_diff, mock_fetch, mock_clone, mock_url, mock_auth, tmp_path,
):
    def side_effect(cmd, cwd, check=True):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        proc.stderr = ""
        return proc

    mock_run_git.side_effect = side_effect

    with patch("scripts.resync.pr_syncer.push_branch") as mock_push:
        r = sync_pr(_PR_OWN, _BASE_CONFIG, dry_run=False, current_user="alice",
                    tracker="github", author_guard=False, work_dir=tmp_path)

    assert r.status == "error"
    assert r.diff_verified is False
    assert "secret.py" in (r.error or "")
    mock_push.assert_not_called()

    # git reset --hard should have been called
    reset_calls = [c for c in mock_run_git.call_args_list if "reset" in c[0][0]]
    assert reset_calls, "git reset --hard should have been called on diff failure"


# ---------------------------------------------------------------------------
# _normalize_gh_pr
# ---------------------------------------------------------------------------

def test_normalize_gh_pr_basic():
    raw = {
        "number": 10,
        "title": "Test PR",
        "url": "https://github.com/org/repo/pull/10",
        "headRefName": "feat/x",
        "baseRefName": "main",
        "author": {"login": "alice"},
        "statusCheckRollup": [{"state": "SUCCESS"}],
        "reviews": [{"state": "APPROVED"}],
        "reviewDecision": "APPROVED",
    }
    pr = _normalize_gh_pr(raw)
    assert pr["number"] == 10
    assert pr["author"] == "alice"
    assert pr["ci_status"] == "pass"
    assert pr["approved_count"] == 1
    assert pr["headRefName"] == "feat/x"


def test_normalize_gh_pr_ci_fail():
    raw = {
        "number": 11, "title": "Broken", "url": "", "headRefName": "fix/y",
        "baseRefName": "main", "author": {"login": "bob"},
        "statusCheckRollup": [{"state": "FAILURE"}], "reviews": [],
        "reviewDecision": "",
    }
    pr = _normalize_gh_pr(raw)
    assert pr["ci_status"] == "fail"
    assert pr["approved_count"] == 0


# ---------------------------------------------------------------------------
# _normalize_bb_pr
# ---------------------------------------------------------------------------

def test_normalize_bb_pr_basic():
    raw = {
        "id": 99,
        "title": "BB PR",
        "links": {"html": {"href": "https://bitbucket.org/ws/repo/pull-requests/99"}},
        "author": {"display_name": "Carol", "account_id": "abc123"},
        "source": {"branch": {"name": "feature/bb"}},
        "destination": {"branch": {"name": "develop"}},
        "participants": [
            {"role": "REVIEWER", "approved": True},
            {"role": "REVIEWER", "approved": False},
        ],
    }
    pr = _normalize_bb_pr(raw)
    assert pr["number"] == 99
    assert pr["author"] == "Carol"
    assert pr["headRefName"] == "feature/bb"
    assert pr["baseRefName"] == "develop"
    assert pr["approved_count"] == 1


# ---------------------------------------------------------------------------
# _fetch_target_prs — explicit mode
# ---------------------------------------------------------------------------

@patch("scripts.resync.run_resync_prs.fetch_prs_by_numbers", return_value=[_PR_OWN])
def test_fetch_target_prs_explicit_by_number(mock_fetch):
    prs, explicit = _fetch_target_prs(
        _BASE_CONFIG, "github", "alice", {}, [], [1]
    )
    assert explicit is True
    assert len(prs) == 1
    mock_fetch.assert_called_once_with(_BASE_CONFIG, [1])


@patch("scripts.resync.run_resync_prs.fetch_prs_by_numbers", return_value=[_PR_OWN])
@patch("scripts.resync.run_resync_prs.parse_pr_url", return_value=("github", 42))
def test_fetch_target_prs_explicit_by_url(mock_parse, mock_fetch):
    prs, explicit = _fetch_target_prs(
        _BASE_CONFIG, "github", "alice", {},
        ["https://github.com/org/repo/pull/42"], [],
    )
    assert explicit is True
    mock_fetch.assert_called_once_with(_BASE_CONFIG, [42])


@patch("scripts.resync.run_resync_prs._fetch_my_open_gh_prs", return_value=[_PR_OWN])
def test_fetch_target_prs_auto_discover_github(mock_gh):
    # --me required to trigger auto-discover; without it, returns empty
    prs, explicit = _fetch_target_prs(
        _BASE_CONFIG, "github", "alice", {}, [], [], me=True
    )
    assert explicit is False
    assert prs == [_PR_OWN]
    mock_gh.assert_called_once()


@patch("scripts.resync.run_resync_prs._fetch_my_open_bb_prs", return_value=[_PR_OTHER])
def test_fetch_target_prs_auto_discover_bitbucket(mock_bb):
    # --me required to trigger auto-discover; without it, returns empty
    prs, explicit = _fetch_target_prs(
        _BASE_CONFIG, "jira/bitbucket", "Carol", {"display_name": "Carol"}, [], [], me=True
    )
    assert explicit is False
    assert prs == [_PR_OTHER]
    mock_bb.assert_called_once()


def test_fetch_target_prs_no_target_returns_empty():
    # No pr_urls, no pr_numbers, no me=True → returns empty (caller must have rejected already)
    prs, explicit = _fetch_target_prs(
        _BASE_CONFIG, "github", "alice", {}, [], [], me=False
    )
    assert prs == []
    assert explicit is False
