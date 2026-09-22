"""Compute blast-radius scope for a PR from touched file paths and CODEOWNERS.

Usage:
    python -m scripts.mq.scope_router --pr-number 42

Required env: GH_ORG, GH_REPO
Reads:  (fetches PR data via gh CLI)
Writes: /workspace/scope.json

Exit codes: 0=done, 1=error, 2=ambiguous scope (3+ unrelated modules)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import click

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.shell import run_cmd
from scripts.mq._shared import SENSITIVE_PATHS, top_level_module

_CODEOWNERS_ENTRY_RE = re.compile(r"^(?!\s*#)(\S+)\s+(.+)$")


def _parse_codeowners(repo_dir: str | None) -> dict[str, list[str]]:
    """Parse CODEOWNERS file and return pattern→owners mapping."""
    if not repo_dir:
        return {}
    for candidate in ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"]:
        path = Path(repo_dir) / candidate
        if path.exists():
            entries: dict[str, list[str]] = {}
            for line in path.read_text().splitlines():
                m = _CODEOWNERS_ENTRY_RE.match(line.strip())
                if m:
                    pattern = m.group(1)
                    owners = [o.strip() for o in m.group(2).split() if o.strip()]
                    entries[pattern] = owners
            return entries
    return {}


def _match_codeowner(filepath: str, codeowners: dict[str, list[str]]) -> list[str]:
    """Return owners for a file path. Last matching pattern wins (CODEOWNERS convention)."""
    matched_owners: list[str] = []
    for pattern, owners in codeowners.items():
        pat = pattern.rstrip("/")
        if pat.startswith("/"):
            pat = pat[1:]
        if filepath.startswith(pat) or filepath == pat:
            matched_owners = owners
        elif "*" in pat:
            regex = pat.replace(".", r"\.").replace("*", ".*")
            if re.match(regex, filepath):
                matched_owners = owners
    return matched_owners


_top_level_module = top_level_module


def _fetch_pr_files(org: str, repo: str, pr_number: str) -> list[dict]:
    """Fetch changed files for a PR via gh CLI."""
    result = run_cmd([
        "gh", "pr", "view", pr_number,
        "--repo", f"{org}/{repo}",
        "--json", "files,additions,deletions",
    ])
    data = json.loads(result.stdout)
    return data.get("files", [])


def _compute_scope(
    files: list[dict],
    codeowners: dict[str, list[str]],
) -> tuple[str, str, list[str], set[str]]:
    """Compute scope name, blast_radius, touched paths, and owner teams."""
    owner_sets: dict[str, set[str]] = {}
    modules: set[str] = set()
    total_lines = 0
    sensitive_touched: list[str] = []

    for f in files:
        path = f.get("path", "")
        additions = f.get("additions", 0)
        deletions = f.get("deletions", 0)
        total_lines += additions + deletions

        module = _top_level_module(path)
        modules.add(module)

        owners = _match_codeowner(path, codeowners)
        for owner in owners:
            owner_sets.setdefault(owner, set()).add(module)

        if SENSITIVE_PATHS.search(path):
            sensitive_touched.append(path)

    all_owners = set(owner_sets.keys())
    n_modules = len(modules)

    if len(owner_sets) == 1:
        scope = list(owner_sets.keys())[0].lstrip("@")
    elif len(owner_sets) > 1:
        scope = "cross-scope"
    elif n_modules == 1:
        scope = list(modules)[0]
    else:
        scope = "cross-scope"

    if total_lines > 300 or n_modules >= 3 or sensitive_touched:
        blast_radius = "high"
    elif total_lines > 50 or n_modules >= 2:
        blast_radius = "medium"
    else:
        blast_radius = "low"

    return scope, blast_radius, sensitive_touched, all_owners


@click.command()
@click.option("--pr-number", required=True, help="PR number to analyze")
def main(pr_number: str) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO"])
    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)

    print(f"[scope_router] pr={pr_number} repo={org}/{repo}", flush=True)

    files = _fetch_pr_files(org, repo, pr_number)
    if not files:
        print("[scope_router] no changed files found", flush=True)
        result = {
            "scope": "empty",
            "blast_radius": "low",
            "touches": [],
            "owners": [],
            "changed_files": 0,
            "lines_changed": 0,
        }
        (workspace / "scope.json").write_text(json.dumps(result, indent=2))
        sys.exit(0)

    codebase_dir = config.get("CODEBASE_DIR", "")
    codeowners = _parse_codeowners(codebase_dir)

    scope, blast_radius, sensitive_touched, owners = _compute_scope(files, codeowners)

    total_lines = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)
    result = {
        "scope": scope,
        "blast_radius": blast_radius,
        "touches": sensitive_touched,
        "owners": sorted(owners),
        "changed_files": len(files),
        "lines_changed": total_lines,
    }

    out_path = workspace / "scope.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"[scope_router] scope={scope} blast_radius={blast_radius} files={len(files)} lines={total_lines}", flush=True)

    try:
        run_cmd([
            "gh", "pr", "edit", pr_number,
            "--repo", f"{org}/{repo}",
            "--add-label", f"scope:{scope}",
        ], check=False)
        print(f"[scope_router] labeled PR with scope:{scope}", flush=True)
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"[scope_router] warn: could not label PR: {e}", flush=True)

    modules = {_top_level_module(f.get("path", "")) for f in files}
    if scope == "cross-scope" and len(modules) >= 3:
        print(f"[scope_router] ambiguous scope: {len(modules)} unrelated modules", flush=True)
        sys.exit(2)


if __name__ == "__main__":
    main()
