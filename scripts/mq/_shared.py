"""Shared utilities for merge-queue scripts.

Centralizes patterns used across scope_router, risk_score, test_impact,
and collect_ready to avoid duplication.
"""
from __future__ import annotations

import json
import re

import requests

from scripts.common.bitbucket_api import _auth as bb_auth, _repo as bb_repo_url
from scripts.common.shell import run_cmd

_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")

_GH_PR_URL_RE = re.compile(
    r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)"
)
_BB_PR_URL_RE = re.compile(
    r"https://bitbucket\.org/([^/]+/[^/]+)/pull-requests?/(\d+)"
)


def _strip_slack_url(ref: str) -> str:
    """Strip Slack's angle-bracket URL formatting: <https://url|display> → https://url."""
    m = re.match(r"^<([^|>]+)(?:\|[^>]*)?>$", ref.strip())
    return m.group(1) if m else ref


def parse_pr_ref(ref: str) -> tuple[str, str | None]:
    """Normalize a PR ref to (pr_number_or_branch, repo_clone_url_or_none).

    Accepts full GitHub/Bitbucket PR URLs, bare numbers, branches, Jira keys,
    or Slack-formatted URLs (<https://...|display>).
    Returns a repo clone URL when the ref is a full URL so callers can pass it to
    apply_repo_override() without requiring a separate --repo flag.

    Examples:
      https://github.com/org/repo/pull/24              → ("24", "https://github.com/org/repo.git")
      https://bitbucket.org/ws/repo/pull-requests/456  → ("456", "https://bitbucket.org/ws/repo.git")
      <https://github.com/org/repo/pull/24|...>        → ("24", "https://github.com/org/repo.git")
      42 / feature/branch / PROJ-123                   → (ref, None)
    """
    ref = _strip_slack_url(ref)
    m = _GH_PR_URL_RE.search(ref)
    if m:
        return m.group(2), f"https://github.com/{m.group(1)}.git"
    m = _BB_PR_URL_RE.search(ref)
    if m:
        return m.group(2), f"https://bitbucket.org/{m.group(1)}.git"
    # Bare repo URL (no PR number) — e.g. https://github.com/org/repo
    m = re.search(r"https://github\.com/([^/]+/[^/.\s]+?)(?:\.git)?$", ref)
    if m:
        return "", f"https://github.com/{m.group(1)}.git"
    m = re.search(r"https://bitbucket\.org/([^/]+/[^/.\s]+?)(?:\.git)?$", ref)
    if m:
        return "", f"https://bitbucket.org/{m.group(1)}.git"
    return ref, None


def apply_repo_override(config: dict, repo_url: str) -> None:
    """Parse a GitHub/Bitbucket URL and inject org/repo fields into config and os.environ.

    Sets GH_ORG + GH_REPO (GitHub) or BITBUCKET_WORKSPACE + BITBUCKET_REPO (Bitbucket)
    so that repo_slug(), resolve_tracker(), and gh/bitbucket CLI calls all pick up the
    correct repo from a full PR URL without requiring separate --repo env vars.

    No-op when repo_url is empty or unrecognised.
    """
    import os
    if not repo_url:
        return
    gh = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", repo_url)
    if gh:
        config["GH_ORG"] = gh.group(1)
        config["GH_REPO"] = gh.group(2)
        os.environ["GH_ORG"] = gh.group(1)
        os.environ["GH_REPO"] = gh.group(2)
        if not config.get("DEFAULT_TRACKER"):
            config["DEFAULT_TRACKER"] = "github"
        return
    bb = re.search(r"bitbucket\.org[:/]([^/]+)/([^/.]+)", repo_url)
    if bb:
        config["BITBUCKET_WORKSPACE"] = bb.group(1)
        config["BITBUCKET_REPO"] = bb.group(2)
        config["DEFAULT_TRACKER"] = "bitbucket"
        os.environ["BITBUCKET_WORKSPACE"] = bb.group(1)
        os.environ["BITBUCKET_REPO"] = bb.group(2)
        os.environ["DEFAULT_TRACKER"] = "bitbucket"


def is_branch_or_tag(ref: str) -> bool:
    """Return True if ref looks like a branch/tag rather than a PR number or Jira key."""
    if not ref:
        return False
    if ref.isdigit():
        return False
    if _JIRA_KEY_RE.match(ref):
        return False
    return True


def resolve_tracker(config: dict, repo_url: str = "") -> str:
    """Resolve effective tracker from repo URL and config. Returns 'github' or 'bitbucket'.

    Vocabulary: this function uses the clone/API vocabulary ('github' | 'bitbucket').
    It is intentionally different from scripts/skill/flags.py:resolve_tracker, which uses
    the Slack-routing vocabulary ('github' | 'jira'). Do not unify them.

    Priority:
      1. repo_url domain  (github.com → github, bitbucket.org → bitbucket)
      2. DEFAULT_TRACKER from config  ('jira' and 'jira/bitbucket' both map to 'bitbucket')
    """
    if repo_url:
        if "github.com" in repo_url:
            return "github"
        if "bitbucket.org" in repo_url:
            return "bitbucket"
    tracker = (config.get("DEFAULT_TRACKER") or "github").lower().strip()
    if tracker in ("jira", "bitbucket", "jira/bitbucket"):
        return "bitbucket"
    return "github"


def repo_slug(config: dict) -> str:
    """Return a display slug like 'org/repo' based on tracker."""
    if resolve_tracker(config) == "bitbucket":
        return f"{config.get('BITBUCKET_WORKSPACE', '')}/{config.get('BITBUCKET_REPO', '')}"
    return f"{config.get('GH_ORG', '')}/{config.get('GH_REPO', '')}"


def resolve_pr_number(config: dict, pr_number: str) -> str | None:
    """Resolve a PR identifier to a numeric PR number.

    If pr_number is already numeric, returns it as-is.
    If it looks like a Jira key (e.g. PROJ-123) and tracker is bitbucket,
    searches Bitbucket PRs by branch name containing the Jira key.
    Returns None if no matching PR is found.
    """
    if not pr_number:
        return None
    if pr_number.isdigit():
        return pr_number
    if resolve_tracker(config) == "bitbucket" and _JIRA_KEY_RE.match(pr_number):
        return _bb_find_pr_by_jira_key(config, pr_number)
    return pr_number


def _bb_find_pr_by_jira_key(config: dict, jira_key: str) -> str | None:
    """Search Bitbucket open PRs for one whose branch contains the Jira key."""
    ws = config.get("BITBUCKET_WORKSPACE", "")
    repo = config.get("BITBUCKET_REPO", "")
    auth = bb_auth(config)
    q = f'source.branch.name~"{jira_key}"'
    url = f"{bb_repo_url(ws, repo)}/pullrequests"
    try:
        resp = requests.get(url, auth=auth, params={"q": q, "pagelen": 5}, timeout=30)
        resp.raise_for_status()
        values = resp.json().get("values", [])
        if values:
            pr_id = values[0].get("id")
            print(f"[mq] resolved {jira_key} → BB PR #{pr_id}", flush=True)
            return str(pr_id)
        for state in ("MERGED", "DECLINED"):
            resp = requests.get(
                url, auth=auth,
                params={"q": q, "state": state, "pagelen": 5},
                timeout=30,
            )
            resp.raise_for_status()
            values = resp.json().get("values", [])
            if values:
                pr_id = values[0].get("id")
                print(f"[mq] resolved {jira_key} → BB PR #{pr_id} ({state})", flush=True)
                return str(pr_id)
    except Exception as e:
        print(f"[mq] warn: BB PR search for {jira_key} failed: {e}", flush=True)
    # Fallback: search by title
    try:
        q_title = f'title~"{jira_key}"'
        resp = requests.get(url, auth=auth, params={"q": q_title, "pagelen": 5}, timeout=30)
        resp.raise_for_status()
        values = resp.json().get("values", [])
        if values:
            pr_id = values[0].get("id")
            print(f"[mq] resolved {jira_key} → BB PR #{pr_id} (by title)", flush=True)
            return str(pr_id)
    except Exception as e:
        print(f"[mq] warn: BB PR title search for {jira_key} failed: {e}", flush=True)
    print(f"[mq] warn: no BB PR found for {jira_key}", flush=True)
    return None


def fetch_changed_files_from_diff(repo_dir: str, base_branch: str = "main") -> list[dict]:
    """Fetch changed files via git diff against a base branch.

    Used when operating on a branch/tag rather than a PR number.
    """
    try:
        result = run_cmd(
            ["git", "diff", "--numstat", f"{base_branch}...HEAD"],
            cwd=repo_dir,
        )
    except Exception:
        result = run_cmd(
            ["git", "diff", "--numstat", f"origin/{base_branch}...HEAD"],
            cwd=repo_dir,
        )
    files = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            adds = int(parts[0]) if parts[0] != "-" else 0
            dels = int(parts[1]) if parts[1] != "-" else 0
            files.append({"path": parts[2], "additions": adds, "deletions": dels})
    return files


def fetch_pr_files(config: dict, pr_number: str) -> list[dict]:
    """Fetch changed files for a PR, branch, or tag.

    Works with GitHub (gh CLI), Bitbucket (REST API), and git diff fallback.
    Accepts: numeric PR ("42"), Jira key ("PROJ-123"), branch ("feature/x"), tag ("v1.2.3").
    """
    repo_dir = config.get("CODEBASE_DIR", "")
    base_branch = config.get("BASE_BRANCH", "main")

    if is_branch_or_tag(pr_number) and repo_dir:
        print(f"[mq] using git diff for branch/tag: {pr_number}", flush=True)
        return fetch_changed_files_from_diff(repo_dir, base_branch)

    if resolve_tracker(config) == "bitbucket":
        resolved = resolve_pr_number(config, pr_number)
        if not resolved:
            print(f"[mq] warn: could not resolve PR {pr_number} — returning empty", flush=True)
            return []
        return _bb_fetch_pr_files(config, resolved)
    return _gh_fetch_pr_files(config, pr_number)


def _gh_fetch_pr_files(config: dict, pr_number: str) -> list[dict]:
    result = run_cmd([
        "gh", "pr", "view", pr_number,
        "--repo", repo_slug(config),
        "--json", "files,additions,deletions",
    ])
    data = json.loads(result.stdout)
    return data.get("files", [])


def _bb_fetch_pr_files(config: dict, pr_number: str) -> list[dict]:
    ws = config.get("BITBUCKET_WORKSPACE", "")
    repo = config.get("BITBUCKET_REPO", "")
    url = f"{bb_repo_url(ws, repo)}/pullrequests/{pr_number}/diffstat"
    auth = bb_auth(config)
    files: list[dict] = []
    while url:
        resp = requests.get(url, auth=auth, timeout=30)
        resp.raise_for_status()
        body = resp.json()
        for v in body.get("values", []):
            new_path = (v.get("new") or {}).get("path", "")
            old_path = (v.get("old") or {}).get("path", "")
            files.append({
                "path": new_path or old_path,
                "additions": v.get("lines_added", 0),
                "deletions": v.get("lines_removed", 0),
            })
        url = body.get("next")
    return files


def fetch_pr_stats(config: dict, pr_number: str) -> dict:
    """Fetch PR stats: files list + aggregate additions/deletions.

    Accepts: numeric PR, Jira key, branch name, or tag.
    """
    repo_dir = config.get("CODEBASE_DIR", "")
    base_branch = config.get("BASE_BRANCH", "main")

    if is_branch_or_tag(pr_number) and repo_dir:
        files = fetch_changed_files_from_diff(repo_dir, base_branch)
        return {
            "files": files,
            "additions": sum(f.get("additions", 0) for f in files),
            "deletions": sum(f.get("deletions", 0) for f in files),
            "changedFiles": len(files),
        }

    if resolve_tracker(config) == "bitbucket":
        resolved = resolve_pr_number(config, pr_number)
        if not resolved:
            return {"files": [], "additions": 0, "deletions": 0}
        files = _bb_fetch_pr_files(config, resolved)
        return {
            "files": files,
            "additions": sum(f.get("additions", 0) for f in files),
            "deletions": sum(f.get("deletions", 0) for f in files),
        }
    result = run_cmd([
        "gh", "pr", "view", pr_number,
        "--repo", repo_slug(config),
        "--json", "files,additions,deletions,changedFiles",
    ])
    return json.loads(result.stdout)


def fetch_ready_prs(config: dict, label: str) -> list[dict]:
    """Fetch open PRs with a label. Bitbucket has no label API — returns empty."""
    if resolve_tracker(config) == "bitbucket":
        print(f"[mq] warn: PR labels not supported on Bitbucket for label={label}", flush=True)
        return []
    result = run_cmd([
        "gh", "pr", "list",
        "--repo", repo_slug(config),
        "--label", label,
        "--state", "open",
        "--json", "number,headRefName,labels,author,createdAt,additions,deletions,files,title",
        "--limit", "100",
    ])
    return json.loads(result.stdout) if result.stdout.strip() else []


def label_pr(config: dict, pr_number: str, label: str) -> None:
    """Add a label to a PR. GitHub-only (Bitbucket has no label API). Skips for branch/tag refs."""
    if resolve_tracker(config) != "github":
        return
    if is_branch_or_tag(pr_number):
        print(f"[mq] skip label: {pr_number} is a branch/tag, not a PR", flush=True)
        return
    try:
        run_cmd([
            "gh", "pr", "edit", pr_number,
            "--repo", repo_slug(config),
            "--add-label", label,
        ], check=False)
    except Exception as e:
        print(f"[mq] warn: could not label PR: {e}", flush=True)


SENSITIVE_PATHS = re.compile(
    r"(^|/)("
    r"auth|security|billing|payments|crypto|secrets|credentials"
    r"|\.env|migrations|rbac|iam|oauth|tokens"
    r")(/|$|\.)",
    re.IGNORECASE,
)

_TRANSPARENT_PREFIXES = ("src", "lib", "pkg", "internal", "crates", "apps")


def top_level_module(filepath: str) -> str:
    """Extract top-level module/directory from a file path.

    For paths under transparent prefixes (src/, lib/, etc.), returns two
    levels deep. Otherwise returns the first path component.
    """
    parts = filepath.split("/")
    if len(parts) >= 2 and parts[0] in _TRANSPARENT_PREFIXES:
        return parts[0] + "/" + parts[1]
    return parts[0]


def is_test_file(path: str) -> bool:
    """Check if a file path looks like a test file."""
    name = path.split("/")[-1] if "/" in path else path
    return (
        name.startswith("test_")
        or name.endswith("_test.go")
        or name.endswith("_test.py")
        or name.endswith("_test.rs")
        or name.endswith(".test.ts")
        or name.endswith(".test.tsx")
        or name.endswith(".test.js")
        or name.endswith(".test.jsx")
        or name.endswith("Test.java")
        or name.endswith("Test.kt")
        or name.endswith("_spec.rb")
        or name.endswith("_test.rb")
        or name.endswith("Tests.cs")
        or name.endswith(".spec.ts")
        or name.endswith(".spec.js")
        or "/tests/" in path
        or "/__tests__/" in path
        or "/test/" in path
        or "/spec/" in path
    )
