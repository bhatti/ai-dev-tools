"""Clone a repo and checkout a PR branch for merge-queue workflows.

Usage:
    python -m scripts.mq.clone_pr --pr-number 42

Required env: GH_ORG + GH_REPO (GitHub) or BITBUCKET_WORKSPACE + BITBUCKET_REPO (Bitbucket)
Writes: /workspace/repo/

Exit codes: 0=done, 1=error
"""
from __future__ import annotations

import subprocess

import click

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.git_utils import clone_by_tracker
from scripts.mq._shared import repo_slug, resolve_tracker


@click.command()
@click.option("--pr-number", default=None, help="PR number to checkout (optional)")
def main(pr_number: str | None) -> None:
    config = load_config(required=[])
    tracker = resolve_tracker(config)
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = workspace / "repo"
    slug = repo_slug(config)

    print(f"[clone_pr] repo={slug} pr={pr_number} tracker={tracker}", flush=True)

    if repo_dir.exists() and (repo_dir / ".git").exists():
        print("[clone_pr] repo already cloned — skipping", flush=True)
    else:
        clone_by_tracker(config, repo_dir)
        print(f"[clone_pr] cloned to {repo_dir}", flush=True)

    if pr_number:
        if tracker == "bitbucket":
            result = subprocess.run(
                ["git", "fetch", "origin",
                 f"refs/pull-requests/{pr_number}/from:pr-{pr_number}"],
                cwd=str(repo_dir), capture_output=True, text=True,
            )
            if result.returncode == 0:
                subprocess.run(["git", "checkout", f"pr-{pr_number}"],
                               cwd=str(repo_dir), capture_output=True, text=True)
                print(f"[clone_pr] checked out PR #{pr_number} via git fetch", flush=True)
            else:
                print(f"[clone_pr] pr checkout failed (non-fatal): {result.stderr.strip()}", flush=True)
        else:
            result = subprocess.run(
                ["gh", "pr", "checkout", pr_number, "--repo", slug],
                cwd=str(repo_dir), capture_output=True, text=True,
            )
            if result.returncode == 0:
                print(f"[clone_pr] checked out PR #{pr_number}", flush=True)
            else:
                print(f"[clone_pr] pr checkout failed (non-fatal): {result.stderr.strip()}", flush=True)


if __name__ == "__main__":
    main()
