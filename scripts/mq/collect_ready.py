"""Collect all PRs labeled ai-merge-ready for merge queue processing.

Usage:
    python -m scripts.mq.collect_ready

Required env: GH_ORG + GH_REPO (GitHub) or BITBUCKET_WORKSPACE + BITBUCKET_REPO (Bitbucket).
              Bitbucket does not support PR labels — returns empty.
Reads:  (fetches PR list via gh CLI or Bitbucket API)
Writes: /workspace/ready_prs.json

Exit codes: 0=done, 1=error
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from scripts.common.config import get_workspace_dir, load_config
from scripts.mq._shared import fetch_ready_prs, repo_slug, top_level_module


_MERGE_READY_LABEL = "ai-merge-ready"


def _compute_age_hours(created_at: str) -> float:
    """Compute PR age in hours from ISO 8601 timestamp."""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds() / 3600
    except (ValueError, AttributeError):
        return 0.0


def _infer_scope(files: list[dict]) -> str:
    """Infer scope from changed file paths (top-level module)."""
    modules: set[str] = set()
    for f in files:
        path = f.get("path", "")
        if path:
            modules.add(top_level_module(path))

    if not modules:
        return "unknown"
    if len(modules) == 1:
        return list(modules)[0]
    if len(modules) <= 2:
        return "multi-module"
    return "cross-scope"


@click.command()
@click.option("--label", default=_MERGE_READY_LABEL, help="Label to filter merge-ready PRs")
def main(label: str) -> None:
    config = load_config(required=[])
    slug = repo_slug(config)
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)

    print(f"[collect_ready] repo={slug} label={label}", flush=True)

    raw_prs = fetch_ready_prs(config, label)
    print(f"[collect_ready] found {len(raw_prs)} PRs with label '{label}'", flush=True)

    ready_prs = []
    for pr in raw_prs:
        files = pr.get("files", [])
        scope = _infer_scope(files)
        additions = pr.get("additions", 0)
        deletions = pr.get("deletions", 0)
        age_hours = _compute_age_hours(pr.get("createdAt", ""))

        # Quick triage — scope_router.py does the authoritative classification
        # after cloning; this is a lightweight pre-sort without repo access.
        total_lines = additions + deletions
        if total_lines > 300 or len(files) >= 10:
            blast_radius = "high"
        elif total_lines > 50 or len(files) >= 3:
            blast_radius = "medium"
        else:
            blast_radius = "low"

        author = pr.get("author", {})
        author_login = author.get("login", "unknown") if isinstance(author, dict) else str(author)

        ready_prs.append({
            "pr_number": pr["number"],
            "title": pr.get("title", ""),
            "scope": scope,
            "blast_radius": blast_radius,
            "author": author_login,
            "age_hours": round(age_hours, 1),
            "additions": additions,
            "deletions": deletions,
            "changed_files": len(files),
            "branch": pr.get("headRefName", ""),
        })

    ready_prs.sort(key=lambda p: p["age_hours"], reverse=True)

    result = {"pr_count": len(ready_prs), "prs": ready_prs}
    out_path = workspace / "ready_prs.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"[collect_ready] wrote {out_path} ({len(ready_prs)} PRs)", flush=True)


if __name__ == "__main__":
    main()
