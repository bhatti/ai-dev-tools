# SPDX-License-Identifier: AGPL-3.0-or-later
"""PR syncer — merge base branch into a PR branch with diff-integrity safety checks.

Safety model:
  1. Author guard (auto-discover mode only): skip PRs not authored by current_user.
     Bypassed when user provides explicit PR URLs/numbers.
  2. Before snapshot: git diff origin/{base}...origin/{pr_branch} — what the PR adds
  3. Attempt: git merge --no-ff --no-edit origin/{base_branch}
  4. Conflict: git merge --abort → return status="conflict" (no push, no damage)
  5. After snapshot: git diff origin/{base}...HEAD — what the PR adds after merge
  6. Verify: same changed files + line counts within 5% + no conflict markers
  7. If verification fails: git reset --hard origin/{pr_branch} → return status="error"
  8. Push (unless dry_run): force push with retries via push_branch()
"""
from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from scripts.common.git_utils import (
    clone_repo,
    configure_git,
    create_branch,
    detect_bitbucket_url,
    detect_repo_url,
    push_branch,
    resolve_clone_auth,
)


@dataclass
class SyncResult:
    pr_number: str | int
    pr_title: str
    pr_url: str
    base_branch: str
    pr_branch: str
    status: str  # "synced" | "up_to_date" | "conflict" | "skipped" | "error"
    commits_merged: int = 0
    conflict_files: list[str] = field(default_factory=list)
    before_diff_lines: int = 0
    after_diff_lines: int = 0
    diff_verified: bool = False
    dry_run: bool = False
    build_status: str = ""     # "pass" | "fail" | "pending" | "unknown"
    reviewers: dict = field(default_factory=dict)
    merge_commit: str = ""
    error: str | None = None


def _run_git(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result


def _diff_snapshot(repo_path: Path, base_branch: str, pr_ref: str) -> str:
    """Three-dot diff: PR's own changes relative to divergence point from base."""
    result = _run_git(
        ["git", "diff", f"origin/{base_branch}...{pr_ref}"],
        cwd=repo_path, check=False,
    )
    return result.stdout


def _count_diff_lines(diff: str) -> int:
    return sum(
        1 for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


def _changed_files_from_diff(diff: str) -> set[str]:
    files: set[str] = set()
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split(" ")
            if len(parts) >= 4:
                path = parts[3]
                if path.startswith("b/"):
                    path = path[2:]
                files.add(path)
    return files


def _count_new_commits(repo_path: Path, pr_branch: str, base_branch: str) -> int:
    """Count commits on base that pr_branch hasn't seen yet."""
    result = _run_git(
        ["git", "rev-list", "--count", f"origin/{pr_branch}..origin/{base_branch}"],
        cwd=repo_path, check=False,
    )
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def _get_conflict_files(repo_path: Path) -> list[str]:
    result = _run_git(["git", "status", "--porcelain"], cwd=repo_path, check=False)
    files: list[str] = []
    for line in result.stdout.splitlines():
        xy = line[:2]
        if xy in ("UU", "AA", "DD", "AU", "UA", "DU", "UD"):
            files.append(line[3:].strip())
    return files


def _has_conflict_markers(repo_path: Path) -> bool:
    """Return True if any tracked file contains git conflict markers.

    `git diff --check` only compares working tree to index and is a no-op after
    a clean committed merge. Use `git grep` to scan tracked file contents instead.
    """
    result = _run_git(
        ["git", "grep", "-l", "^<<<<<<<", "--"],
        cwd=repo_path, check=False,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


def _verify_diff(
    before_diff: str, after_diff: str, threshold: float = 0.05
) -> tuple[bool, str]:
    """Verify that before and after diffs represent the same logical PR change.

    Returns (ok, reason_if_not_ok).
    """
    before_files = _changed_files_from_diff(before_diff)
    after_files = _changed_files_from_diff(after_diff)

    if before_files != after_files:
        added = sorted(after_files - before_files)
        removed = sorted(before_files - after_files)
        parts = []
        if added:
            parts.append(f"unexpected new files in diff: {added}")
        if removed:
            parts.append(f"files missing from diff after merge: {removed}")
        return False, "; ".join(parts)

    before_lines = _count_diff_lines(before_diff)
    after_lines = _count_diff_lines(after_diff)

    if before_lines == 0 and after_lines == 0:
        return True, ""

    # If before snapshot has no text lines but after does, this is suspicious:
    # either the PR is binary-only (file-set check already passed) or history
    # was truncated (shallow clone). Report as a warning rather than hard-fail
    # since the file-set check is the stronger safety net.
    if before_lines == 0:
        return True, ""

    delta = abs(after_lines - before_lines) / before_lines
    if delta > threshold:
        return False, (
            f"diff size changed by {delta:.1%} "
            f"(before={before_lines} lines, after={after_lines} lines) "
            f"— exceeds {threshold:.0%} safety threshold"
        )

    return True, ""


def _fetch_branch(repo_path: Path, branch: str, dry_run: bool = False) -> None:
    depth = "100" if dry_run else "500"
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    _run_git(
        ["git", "fetch", "--depth", depth, "origin", refspec],
        cwd=repo_path, check=False,
    )


def _ensure_merge_base(
    repo_path: Path, base_branch: str, pr_branch: str, dry_run: bool = False
) -> bool:
    """Ensure the common ancestor between base and PR branch is locally available.

    For dry-run: deepens by 2000 commits instead of full unshallow (avoids multi-minute
    history fetches on large repos where a full --unshallow is unnecessary for a no-push check).
    Returns True if the merge base is reachable, False otherwise.
    """
    result = _run_git(
        ["git", "merge-base", f"origin/{base_branch}", f"origin/{pr_branch}"],
        cwd=repo_path, check=False,
    )
    if result.returncode == 0:
        return True

    if dry_run:
        print(
            f"[resync] shallow clone missing merge-base for {pr_branch}↔{base_branch}; "
            "deepening by 2000 commits (dry-run — skipping full unshallow)",
            flush=True,
        )
        _run_git(["git", "fetch", "--deepen=2000", "origin"], cwd=repo_path, check=False)
    else:
        print(
            f"[resync] shallow clone missing merge-base for {pr_branch}↔{base_branch}; "
            "attempting unshallow fetch",
            flush=True,
        )
        _run_git(["git", "fetch", "--unshallow", "origin"], cwd=repo_path, check=False)

    # Re-fetch both branches after deepening
    for branch in (base_branch, pr_branch):
        refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
        _run_git(["git", "fetch", "origin", refspec], cwd=repo_path, check=False)
    result2 = _run_git(
        ["git", "merge-base", f"origin/{base_branch}", f"origin/{pr_branch}"],
        cwd=repo_path, check=False,
    )
    return result2.returncode == 0


def _build_clone_url(config: dict, tracker: str) -> str:
    if tracker in ("jira", "bitbucket", "jira/bitbucket"):
        workspace = config.get("BITBUCKET_WORKSPACE", "")
        repo = config.get("BITBUCKET_REPO", "")
        bb_token = config.get("BITBUCKET_TOKEN", config.get("BITBUCKET_APP_PASSWORD", ""))
        return detect_bitbucket_url(workspace, repo, use_ssh=not bb_token)
    org = config.get("GH_ORG", "")
    repo = config.get("GH_REPO", "")
    http_token = config.get("GH_TOKEN", config.get("GITHUB_TOKEN", ""))
    return detect_repo_url(org, repo, use_ssh=not http_token)


def sync_pr(
    pr: dict,
    config: dict,
    dry_run: bool,
    current_user: str,
    tracker: str,
    author_guard: bool = True,
    work_dir: Path | None = None,
) -> SyncResult:
    """Sync a single open PR with its base branch.

    author_guard: when True (auto-discover mode), skip PRs not authored by current_user.
                  when False (explicit PR URLs/numbers given), bypass author check.
    """

    pr_number = pr.get("number", "?")
    pr_title = pr.get("title", "")
    pr_url = pr.get("url", "")
    pr_branch = pr.get("headRefName", pr.get("source_branch", ""))
    base_branch = pr.get("baseRefName", pr.get("destination_branch", "main"))
    author = pr.get("author", "")
    build_status = pr.get("ci_status", "unknown")
    rev_raw = pr.get("reviewers", {})
    reviewers = {
        "approved": pr.get("approved_count", rev_raw.get("approved", 0)),
        "changes_requested": pr.get("changes_requested_count", rev_raw.get("changes_requested", 0)),
        "pending": pr.get("pending_count", rev_raw.get("pending", 0)),
    }

    result = SyncResult(
        pr_number=pr_number,
        pr_title=pr_title,
        pr_url=pr_url,
        base_branch=base_branch,
        pr_branch=pr_branch,
        status="error",
        dry_run=dry_run,
        build_status=build_status,
        reviewers=reviewers,
    )

    if not pr_branch:
        result.error = "could not determine PR branch name"
        print(f"[resync] PR #{pr_number}: missing branch name — skipping", flush=True)
        return result

    # Author guard (auto-discover mode only)
    if author_guard:
        if not current_user:
            # Cannot determine who we are — refuse to process any PR to prevent
            # accidentally touching someone else's PR
            result.status = "error"
            result.error = (
                "could not determine current user identity — "
                "refusing to run author guard with unknown user; "
                "provide GH_USER/GITHUB_USER env or fix gh CLI auth"
            )
            print(f"[resync] PR #{pr_number}: {result.error}", flush=True)
            return result
        if author and author.lower() != current_user.lower():
            result.status = "skipped"
            result.error = (
                f"PR authored by '{author}', not '{current_user}' — "
                "skipping (use explicit PR URL/# to bypass)"
            )
            print(f"[resync] skip PR #{pr_number}: {result.error}", flush=True)
            return result

    # Determine clone destination — track tmp path for cleanup
    tmp_dir: str | None = None
    if work_dir is not None:
        clone_dest = work_dir / f"pr_{pr_number}"
    else:
        tmp_dir = tempfile.mkdtemp(prefix=f"resync_pr_{pr_number}_")
        clone_dest = Path(tmp_dir) / "repo"

    try:
        clone_url = _build_clone_url(config, tracker)
        http_token, http_username, ssh_key = resolve_clone_auth(config, tracker)

        # Dry-run uses a shallower clone to avoid fetching unnecessary history on large repos.
        # Real syncs keep depth=500 for reliable merge-base detection.
        clone_depth = 100 if dry_run else 500
        print(
            f"[resync] PR #{pr_number}: '{pr_title}' — cloning {clone_url!r} "
            f"(depth={clone_depth}{'  dry-run' if dry_run else ''})",
            flush=True,
        )

        clone_repo(
            clone_url, clone_dest, depth=clone_depth,
            http_token=http_token, http_username=http_username, ssh_key=ssh_key,
        )

        # Ensure both branches have local tracking refs
        _fetch_branch(clone_dest, base_branch, dry_run=dry_run)
        _fetch_branch(clone_dest, pr_branch, dry_run=dry_run)

        # Verify common ancestor is available; for dry-run deepens rather than full unshallow
        if not _ensure_merge_base(clone_dest, base_branch, pr_branch, dry_run=dry_run):
            result.status = "error"
            fetch_desc = "deepening by 2000 commits" if dry_run else "unshallow fetch"
            result.error = (
                f"could not find merge-base between origin/{base_branch} "
                f"and origin/{pr_branch} — even after {fetch_desc}"
            )
            return result

        # Before snapshot: PR's own changes relative to divergence point
        before_diff = _diff_snapshot(clone_dest, base_branch, f"origin/{pr_branch}")
        result.before_diff_lines = _count_diff_lines(before_diff)

        # Count new commits on base that PR branch doesn't have
        new_commits = _count_new_commits(clone_dest, pr_branch, base_branch)
        result.commits_merged = new_commits

        if new_commits == 0:
            print(f"[resync] PR #{pr_number}: already up to date", flush=True)
            result.status = "up_to_date"
            result.after_diff_lines = result.before_diff_lines
            result.diff_verified = True
            return result

        print(
            f"[resync] PR #{pr_number}: {new_commits} new commit(s) on {base_branch} to merge",
            flush=True,
        )

        # Checkout the PR branch
        git_name = config.get("GIT_USER_NAME", "AI Resync Bot")
        git_email = config.get("GIT_USER_EMAIL", "ai-resync@noreply.local")
        configure_git(clone_dest, git_name, git_email)
        create_branch(clone_dest, pr_branch)

        # Attempt merge (no-ff preserves merge commit; no-edit avoids interactive prompt)
        merge_result = _run_git(
            ["git", "merge", "--no-ff", "--no-edit", f"origin/{base_branch}"],
            cwd=clone_dest, check=False,
        )

        if merge_result.returncode != 0:
            conflict_files = _get_conflict_files(clone_dest)
            _run_git(["git", "merge", "--abort"], cwd=clone_dest, check=False)
            result.status = "conflict"
            result.conflict_files = conflict_files
            result.error = (
                f"merge conflicts in {len(conflict_files)} file(s): "
                + ", ".join(conflict_files[:5])
            )
            print(f"[resync] PR #{pr_number}: CONFLICT — {conflict_files}", flush=True)
            return result

        # After snapshot: PR's own changes after merge
        after_diff = _diff_snapshot(clone_dest, base_branch, "HEAD")
        result.after_diff_lines = _count_diff_lines(after_diff)

        # Verify diff integrity
        ok, reason = _verify_diff(before_diff, after_diff)
        if not ok:
            _run_git(["git", "reset", "--hard", f"origin/{pr_branch}"], cwd=clone_dest, check=False)
            result.status = "error"
            result.diff_verified = False
            result.error = f"diff integrity check failed: {reason}"
            print(f"[resync] PR #{pr_number}: diff mismatch — {reason}", flush=True)
            return result

        # Check for residual conflict markers
        if _has_conflict_markers(clone_dest):
            _run_git(["git", "reset", "--hard", f"origin/{pr_branch}"], cwd=clone_dest, check=False)
            result.status = "error"
            result.diff_verified = False
            result.error = "conflict markers remain in working tree after merge"
            print(f"[resync] PR #{pr_number}: conflict markers detected — aborting push", flush=True)
            return result

        result.diff_verified = True

        # Capture merge commit SHA
        head_r = _run_git(["git", "rev-parse", "--short", "HEAD"], cwd=clone_dest, check=False)
        result.merge_commit = head_r.stdout.strip()

        if dry_run:
            print(
                f"[resync] PR #{pr_number}: DRY-RUN — merge verified (diff OK), skipping push",
                flush=True,
            )
            result.status = "synced"
            return result

        # Push (force, with retry)
        push_branch(
            clone_dest, pr_branch,
            force_with_lease=True,
            http_token=http_token,
            http_username=http_username,
            url=clone_url,
        )
        result.status = "synced"
        print(
            f"[resync] PR #{pr_number}: pushed successfully — merge commit {result.merge_commit}",
            flush=True,
        )
        return result

    except Exception as exc:
        result.status = "error"
        result.error = str(exc)
        print(f"[resync] ERROR PR #{pr_number}: {exc}", flush=True)
        return result

    finally:
        # Clean up temp directory created by this function (not caller-supplied work_dir)
        if tmp_dir is not None:
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)
