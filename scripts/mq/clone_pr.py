"""Clone a repo and checkout a PR branch for merge-queue workflows.

Usage:
    python -m scripts.mq.clone_pr --pr-number 42
    python -m scripts.mq.clone_pr --pr-number feature/my-branch
    python -m scripts.mq.clone_pr --pr-number v1.2.3
    python -m scripts.mq.clone_pr --repo https://github.com/org/repo --branch main

Required env: GH_ORG + GH_REPO (GitHub) or BITBUCKET_WORKSPACE + BITBUCKET_REPO (Bitbucket)
Writes: /workspace/repo/

Exit codes: 0=done, 1=error
"""
from __future__ import annotations

import os
import re
import subprocess

import click

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.git_utils import clone_by_tracker
from scripts.mq._shared import is_branch_or_tag, repo_slug, resolve_pr_number, resolve_tracker


def _apply_repo_override(config: dict, repo_url: str) -> None:
    """Parse a GitHub/Bitbucket URL and override config org/repo fields."""
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
        print(f"[clone_pr] repo override: {gh.group(1)}/{gh.group(2)}", flush=True)
        return
    bb = re.search(r"bitbucket\.org[:/]([^/]+)/([^/.]+)", repo_url)
    if bb:
        config["BITBUCKET_WORKSPACE"] = bb.group(1)
        config["BITBUCKET_REPO"] = bb.group(2)
        os.environ["BITBUCKET_WORKSPACE"] = bb.group(1)
        os.environ["BITBUCKET_REPO"] = bb.group(2)
        print(f"[clone_pr] repo override: {bb.group(1)}/{bb.group(2)}", flush=True)


@click.command()
@click.option("--pr-number", default=None, help="PR number, branch, or tag to checkout")
@click.option("--repo", default=None, help="Repository URL (overrides GH_ORG/GH_REPO)")
@click.option("--branch", default=None, help="Branch name (used when --pr-number is empty)")
def main(pr_number: str | None, repo: str | None, branch: str | None) -> None:
    config = load_config(required=[])

    _apply_repo_override(config, repo)

    if not pr_number and branch:
        pr_number = branch

    tracker = resolve_tracker(config)
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    repo_dir = workspace / "repo"
    slug = repo_slug(config)

    print(f"[clone_pr] repo={slug} ref={pr_number} tracker={tracker}", flush=True)

    if repo_dir.exists() and (repo_dir / ".git").exists():
        print("[clone_pr] repo already cloned — skipping", flush=True)
    else:
        clone_by_tracker(config, repo_dir)
        print(f"[clone_pr] cloned to {repo_dir}", flush=True)

    if not pr_number:
        return

    if is_branch_or_tag(pr_number):
        subprocess.run(
            ["git", "fetch", "origin", pr_number],
            cwd=str(repo_dir), capture_output=True, text=True,
        )
        result = subprocess.run(
            ["git", "checkout", pr_number],
            cwd=str(repo_dir), capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"[clone_pr] checked out branch/tag: {pr_number}", flush=True)
        else:
            result = subprocess.run(
                ["git", "checkout", f"origin/{pr_number}"],
                cwd=str(repo_dir), capture_output=True, text=True,
            )
            if result.returncode == 0:
                print(f"[clone_pr] checked out origin/{pr_number}", flush=True)
            else:
                print(f"[clone_pr] checkout failed: {result.stderr.strip()}", flush=True)
        return

    if tracker == "bitbucket":
        resolved = resolve_pr_number(config, pr_number)
        if not resolved:
            print(f"[clone_pr] could not resolve {pr_number} to BB PR — skipping checkout", flush=True)
            return
        result = subprocess.run(
            ["git", "fetch", "origin",
             f"refs/pull-requests/{resolved}/from:pr-{resolved}"],
            cwd=str(repo_dir), capture_output=True, text=True,
        )
        if result.returncode == 0:
            subprocess.run(["git", "checkout", f"pr-{resolved}"],
                           cwd=str(repo_dir), capture_output=True, text=True)
            print(f"[clone_pr] checked out PR #{resolved} via git fetch", flush=True)
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
