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


def resolve_tracker(config: dict) -> str:
    """Resolve effective tracker from config. Returns 'github' or 'bitbucket'."""
    tracker = (config.get("DEFAULT_TRACKER") or "github").lower().strip()
    if tracker in ("jira", "bitbucket", "jira/bitbucket"):
        return "bitbucket"
    return "github"


def repo_slug(config: dict) -> str:
    """Return a display slug like 'org/repo' based on tracker."""
    if resolve_tracker(config) == "bitbucket":
        return f"{config.get('BITBUCKET_WORKSPACE', '')}/{config.get('BITBUCKET_REPO', '')}"
    return f"{config.get('GH_ORG', '')}/{config.get('GH_REPO', '')}"


def fetch_pr_files(config: dict, pr_number: str) -> list[dict]:
    """Fetch changed files for a PR. Returns list of dicts with path/additions/deletions.

    Works with both GitHub (gh CLI) and Bitbucket (REST API).
    """
    if resolve_tracker(config) == "bitbucket":
        return _bb_fetch_pr_files(config, pr_number)
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
    """Fetch PR stats: files list + aggregate additions/deletions."""
    if resolve_tracker(config) == "bitbucket":
        files = _bb_fetch_pr_files(config, pr_number)
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
    """Add a label to a PR. GitHub-only (Bitbucket has no label API)."""
    if resolve_tracker(config) != "github":
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
