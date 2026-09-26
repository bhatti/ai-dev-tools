"""Collect open PRs for merge queue analysis — no label required.

Usage:
    python -m scripts.mq.collect_ready                         # single repo (GH_ORG/GH_REPO or BB_WORKSPACE/BB_REPO)
    python -m scripts.mq.collect_ready --repo bhatti/todo-sample  # explicit repo override
    python -m scripts.mq.collect_ready --label ready-to-merge    # optional label filter (GH only)

Works for GitHub and Bitbucket via existing standup fetchers (gather_gh / bb_helpers).
Tracker resolved via DEFAULT_TRACKER or repo URL domain — same logic as all mq scripts.

Reads:  open PR list via gh CLI or Bitbucket REST (no special label needed)
Writes: /workspace/ready_prs.json

Exit codes: 0=done, 1=error
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import click

from scripts.common.config import get_workspace_dir, load_config
from scripts.mq._shared import (
    _cfg_for_repo,
    fetch_open_prs,
    repo_slug,
)


def _compute_age_hours(created_at: str) -> float:
    """Compute PR age in hours from ISO 8601 timestamp."""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds() / 3600
    except (ValueError, AttributeError):
        return 0.0


def _normalize_pr(pr: dict, default_repo: str) -> dict:
    """Normalize a PR dict from gather_gh or bb_helpers to the ready_prs schema.

    gather_gh returns: id (=number), title, author (login str), branch, created,
        age_hours, reviewers, has_approval, approval_count, ci_status, url, labels
    bb_helpers returns: id, title, author (display_name str), branch, created,
        age_hours, reviewers
    Neither returns files/additions/deletions — blast_radius defaults to 'low'.
    scope_router.py does authoritative per-PR file analysis downstream.
    """
    pr_number = pr.get("number") or pr.get("id") or 0

    # age_hours already computed by both standup fetchers; fall back to timestamp parse
    age_hours = pr.get("age_hours") or _compute_age_hours(
        pr.get("createdAt", "") or pr.get("created", "")
    )

    # author: gather_gh returns login string; bb_helpers returns display_name string
    author = pr.get("author", "")
    if isinstance(author, dict):
        author_login = author.get("login") or author.get("display_name") or "unknown"
    else:
        author_login = str(author) or "unknown"

    # ci_status: gather_gh computes it; bb_helpers doesn't (no BB CI status API used)
    ci_status = pr.get("ci_status", "none")

    # approval: gather_gh has has_approval + approval_count; bb_helpers has neither yet
    has_approval = pr.get("has_approval") or (pr.get("approval_count", 0) > 0)

    return {
        "pr_number": pr_number,
        "repo": pr.get("repo", default_repo),
        "title": pr.get("title", ""),
        "scope": "unknown",   # scope_router.py computes authoritative scope per PR
        "blast_radius": "low",  # risk_score.py computes authoritative risk per PR
        "author": author_login,
        "age_hours": round(float(age_hours), 1),
        "branch": pr.get("headRefName", "") or pr.get("branch", ""),
        "ci_status": ci_status,
        "has_approval": bool(has_approval),
        "url": pr.get("url", ""),
        "labels": pr.get("labels", []),
    }


@click.command()
@click.option("--label", default="", help="Optional label filter (empty = all open PRs; GH only)")
@click.option("--repo", default="", help="Repo override: full URL or org/repo slug")
def main(label: str, repo: str) -> None:
    config = load_config(required=[])

    # _cfg_for_repo sets GH_ORG/GH_REPO (or BB equivalents) on a copy; merge it back
    # so repo_slug() and the artifact output reflect the actual repo that was queried.
    if repo:
        config = _cfg_for_repo(config, repo)

    raw_prs = fetch_open_prs(config, label=label)
    slug = repo_slug(config)
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)

    print(f"[collect_ready] repo={slug} label={label!r} prs_found={len(raw_prs)}", flush=True)

    ready_prs = [_normalize_pr(pr, slug) for pr in raw_prs]
    ready_prs.sort(key=lambda p: p["age_hours"], reverse=True)

    result = {
        "pr_count": len(ready_prs),
        "repo": slug,
        "prs": ready_prs,
    }
    out_path = workspace / "ready_prs.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"[collect_ready] wrote {out_path} ({len(ready_prs)} PRs)", flush=True)
    print(f"::add-task-context TOTAL_PRS::{len(ready_prs)}", flush=True)


if __name__ == "__main__":
    main()
