"""Shared repo URL resolution, labelling, and cloning helpers.

Extracted from run_codebase_audit.py so that both codebase-audit and pr-audit
(and any future repo-based workflows) share the same logic.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path


def resolve_repo_url(config: dict, explicit_url: str | None = None) -> str | None:
    """Return the git clone URL for the target repo, or None if unresolvable.

    Resolution order:
      1. explicit --repo-url flag
      2. CODEBASE_REPO_URL env
      3. BITBUCKET_WORKSPACE + BITBUCKET_REPO (if both set — works regardless of DEFAULT_TRACKER)
      4. GH_ORG + GH_REPO (if both set)
    DEFAULT_TRACKER is used as a tiebreaker when both Bitbucket and GitHub vars are set.
    """
    if explicit_url:
        return explicit_url

    env_url = config.get("CODEBASE_REPO_URL", "").strip()
    if env_url:
        return env_url

    tracker = (config.get("DEFAULT_TRACKER") or "").lower().strip()

    # ── Bitbucket detection ────────────────────────────────────────────────────
    bb_workspace = config.get("BITBUCKET_WORKSPACE", "").strip()
    bb_repo = config.get("BITBUCKET_REPO", "").strip()
    has_bitbucket = bool(bb_workspace and bb_repo)

    # ── GitHub detection ───────────────────────────────────────────────────────
    gh_org = config.get("GH_ORG", "").strip()
    gh_repo = config.get("GH_REPO", "").strip()
    has_github = bool(gh_org and gh_repo)

    # Prefer the tracker-specified provider; fall back to whichever has credentials
    prefer_bitbucket = tracker in ("jira", "jira/bitbucket", "bitbucket") or (has_bitbucket and not has_github)

    if prefer_bitbucket and has_bitbucket:
        token = config.get("BITBUCKET_TOKEN", config.get("BITBUCKET_APP_PASSWORD", "")).strip()
        user = config.get("BITBUCKET_USERNAME", "").strip()
        if token and user:
            auth = f"x-token-auth:{token}@" if token.startswith("ATATT") else f"{user}:{token}@"
        else:
            auth = ""
        return f"https://{auth}bitbucket.org/{bb_workspace}/{bb_repo}.git"

    if has_github:
        token = config.get("GH_TOKEN", config.get("GITHUB_TOKEN", "")).strip()
        auth = f"x-token-auth:{token}@" if token else ""
        return f"https://{auth}github.com/{gh_org}/{gh_repo}.git"

    if has_bitbucket:
        token = config.get("BITBUCKET_TOKEN", config.get("BITBUCKET_APP_PASSWORD", "")).strip()
        user = config.get("BITBUCKET_USERNAME", "").strip()
        if token and user:
            auth = f"x-token-auth:{token}@" if token.startswith("ATATT") else f"{user}:{token}@"
        else:
            auth = ""
        return f"https://{auth}bitbucket.org/{bb_workspace}/{bb_repo}.git"

    return None


def repo_label(config: dict, repo_url: str | None) -> str:
    """Return a human-readable label like 'org/repo' from the URL or config."""
    if repo_url:
        # Strip credentials before extracting path: https://user:token@host/org/repo.git
        clean = re.sub(r'https?://[^@]+@', 'https://', repo_url)
        m = re.search(r'/([^/]+/[^/]+?)(?:\.git)?$', clean)
        if m:
            return m.group(1)
        # SSH URL: git@host:org/repo.git
        m = re.search(r':([^/]+/[^/]+?)(?:\.git)?$', clean)
        if m:
            return m.group(1)
    workspace = config.get("BITBUCKET_WORKSPACE", "") or config.get("GH_ORG", "")
    repo = config.get("BITBUCKET_REPO", "") or config.get("GH_REPO", "")
    if workspace and repo:
        return f"{workspace}/{repo}"
    return "unknown/repo"


def clone_for_audit(repo_url: str, branch: str, dest: Path, depth: int = 500) -> tuple[bool, str]:
    """Shallow-clone the repo into dest. Returns (success, actual_branch)."""
    dest.mkdir(parents=True, exist_ok=True)
    # Try with the specified branch first
    for try_branch in [branch, None]:
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        cmd = ["git", "clone", "--depth", str(depth)]
        if try_branch:
            cmd += ["--branch", try_branch]
        cmd += [repo_url, str(dest)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=300, check=True)
            # Detect actual branch from the clone
            res = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=dest, capture_output=True, text=True,
            )
            actual = res.stdout.strip() or try_branch or branch
            if try_branch != branch:
                print(f"[audit] branch '{branch}' not found — cloned default branch '{actual}'", flush=True)
            return True, actual
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode(errors="replace")[:500] if e.stderr else ""
            if try_branch and "not found" in stderr.lower():
                print(f"[audit] branch '{try_branch}' not found, retrying with default branch...", flush=True)
                continue
            print(f"[audit] clone failed: {stderr}", file=sys.stderr, flush=True)
            return False, branch
        except subprocess.TimeoutExpired:
            print("[audit] clone timed out", file=sys.stderr, flush=True)
            return False, branch
    return False, branch
