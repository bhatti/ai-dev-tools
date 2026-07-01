"""Clean stale workspaces and merged/closed AI branches.

Usage:
    python -m scripts.gh.cleanup

Required env: GH_ORG, GH_REPO, GH_TOKEN
Optional env: WORKSPACE_DIR, WORKSPACE_MAX_AGE_HOURS (default: 4)

Writes: /workspace/cleanup_report.txt
Exit codes: 0=success, 1=error
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import click

from scripts.common.config import load_config


def _run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def list_merged_ai_branches(org: str, repo: str) -> list[str]:
    """Return ai/* branches whose PRs are merged or closed."""
    result = _run([
        "gh", "api",
        f"repos/{org}/{repo}/branches",
        "--paginate",
        "--jq", '[.[] | select(.name | startswith("ai/")) | .name]',
    ])
    if result.returncode != 0:
        print(f"WARNING: could not list branches: {result.stderr.strip()}", file=sys.stderr)
        return []

    import json
    try:
        branches = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []

    merged = []
    for branch in branches:
        pr_result = _run([
            "gh", "pr", "list",
            "-R", f"{org}/{repo}",
            "--head", branch,
            "--state", "all",
            "--json", "state,mergedAt",
            "--jq", ".[0]",
        ])
        if pr_result.returncode != 0:
            continue
        pr_data_str = pr_result.stdout.strip()
        if not pr_data_str:
            continue
        try:
            pr_data = json.loads(pr_data_str)
        except json.JSONDecodeError:
            continue
        state = pr_data.get("state", "OPEN").upper()
        merged_at = pr_data.get("mergedAt")
        if state in ("MERGED", "CLOSED") or merged_at:
            merged.append(branch)

    return merged


def delete_remote_branch(org: str, repo: str, branch: str) -> bool:
    result = _run([
        "gh", "api",
        "-X", "DELETE",
        f"repos/{org}/{repo}/git/refs/heads/{branch}",
    ])
    if result.returncode == 0:
        print(f"  Deleted remote branch: {branch}")
        return True
    else:
        print(f"  WARNING: could not delete {branch}: {result.stderr.strip()}", file=sys.stderr)
        return False


def clean_stale_workspace_dirs(workspace_dir: Path, max_age_hours: float) -> list[str]:
    """Remove workspace subdirectories older than max_age_hours. Returns list of removed dirs."""
    removed = []
    cutoff = time.time() - max_age_hours * 3600
    if not workspace_dir.is_dir():
        return removed
    for child in workspace_dir.iterdir():
        if not child.is_dir():
            continue
        mtime = child.stat().st_mtime
        if mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)
            removed.append(child.name)
            print(f"  Removed stale workspace: {child.name}")
    return removed


@click.command()
def main() -> None:
    config = load_config(required=["GH_ORG", "GH_REPO", "GH_TOKEN"])
    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    workspace_dir = Path(config.get("WORKSPACE_DIR", "/workspace"))
    max_age_hours = float(config.get("WORKSPACE_MAX_AGE_HOURS", "4"))

    report_lines = [f"=== Cleanup report for {org}/{repo} ===\n"]

    # 1. Delete merged/closed AI branches
    print(f"Scanning ai/* branches in {org}/{repo} ...")
    to_delete = list_merged_ai_branches(org, repo)
    report_lines.append(f"Merged/closed branches to delete: {len(to_delete)}\n")
    deleted = 0
    for branch in to_delete:
        if delete_remote_branch(org, repo, branch):
            deleted += 1
            report_lines.append(f"  deleted: {branch}\n")
        else:
            report_lines.append(f"  failed:  {branch}\n")
    report_lines.append(f"Branches deleted: {deleted}/{len(to_delete)}\n\n")

    # 2. Clean stale workspace directories
    print(f"Cleaning workspace dirs older than {max_age_hours}h in {workspace_dir} ...")
    removed = clean_stale_workspace_dirs(workspace_dir, max_age_hours)
    report_lines.append(f"Stale workspaces removed: {len(removed)}\n")
    for d in removed:
        report_lines.append(f"  removed: {d}\n")

    report_path = workspace_dir / "cleanup_report.txt"
    try:
        report_path.write_text("".join(report_lines))
        print(f"Report written to {report_path}")
    except OSError as e:
        print(f"WARNING: could not write report: {e}", file=sys.stderr)

    print(f"Cleanup done: {deleted} branch(es) deleted, {len(removed)} workspace(s) removed")


if __name__ == "__main__":
    main()
