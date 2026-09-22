"""RADAR-style risk tier from scope, diff stats, and historical signals.

Usage:
    python -m scripts.mq.risk_score --pr-number 42

Required env: GH_ORG + GH_REPO (GitHub) or BITBUCKET_WORKSPACE + BITBUCKET_REPO (Bitbucket)
Reads:  /workspace/scope.json (from scope_router)
Writes: /workspace/risk_score.json

Exit codes: 0=done (low/medium risk), 1=error
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from scripts.common.config import get_workspace_dir, load_config

from scripts.mq._shared import SENSITIVE_PATHS, is_test_file as _is_test_file

_BLAST_RADIUS_SCORES = {"low": 2, "medium": 5, "high": 9}

_RISK_TIERS = [
    (15, "LOW"),
    (30, "MEDIUM"),
    (100, "HIGH"),
]


def _score_size(additions: int, deletions: int) -> int:
    """Score PR size on a 0-10 scale based on total lines changed."""
    total = additions + deletions
    if total <= 20:
        return 1
    if total <= 50:
        return 2
    if total <= 100:
        return 3
    if total <= 200:
        return 5
    if total <= 500:
        return 7
    if total <= 1000:
        return 9
    return 10


def _score_file_count(n_files: int) -> int:
    """Score number of changed files on a 0-10 scale."""
    if n_files <= 3:
        return 1
    if n_files <= 5:
        return 2
    if n_files <= 10:
        return 4
    if n_files <= 20:
        return 6
    if n_files <= 50:
        return 8
    return 10


def _score_sensitive_paths(files: list[dict]) -> int:
    """Score presence of sensitive path changes on a 0-10 scale."""
    sensitive_count = sum(
        1 for f in files if SENSITIVE_PATHS.search(f.get("path", ""))
    )
    if sensitive_count == 0:
        return 0
    if sensitive_count <= 2:
        return 5
    if sensitive_count <= 5:
        return 7
    return 10


def _score_test_coverage(files: list[dict]) -> int:
    """Score test coverage based on ratio of test files to source files.

    Lower score = better coverage. High score = risk of untested changes.
    """
    test_files = [f for f in files if _is_test_file(f.get("path", ""))]
    src_files = [f for f in files if not _is_test_file(f.get("path", ""))]

    if not src_files:
        return 0

    ratio = len(test_files) / len(src_files) if src_files else 1.0
    if ratio >= 1.0:
        return 0
    if ratio >= 0.5:
        return 2
    if ratio >= 0.25:
        return 4
    if ratio > 0:
        return 6
    return 8


def _score_historical(workspace: Path) -> int:
    """Score based on historical defect data if available."""
    history_path = workspace / "defect_history.json"
    if not history_path.exists():
        return 3

    try:
        data = json.loads(history_path.read_text())
        defect_rate = data.get("recent_defect_rate", 0.0)
        if defect_rate <= 0.01:
            return 1
        if defect_rate <= 0.05:
            return 3
        if defect_rate <= 0.10:
            return 6
        return 9
    except (json.JSONDecodeError, OSError):
        return 3


def _tier_for_score(score: int) -> str:
    """Map composite score to risk tier."""
    for threshold, tier in _RISK_TIERS:
        if score <= threshold:
            return tier
    return "HIGH"


@click.command()
@click.option("--pr-number", required=True, help="PR number to assess risk")
def main(pr_number: str) -> None:
    config = load_config(required=[])
    from scripts.mq._shared import fetch_pr_stats, repo_slug
    slug = repo_slug(config)
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)

    print(f"[risk_score] pr={pr_number} repo={slug}", flush=True)

    scope_path = workspace / "scope.json"
    scope_data: dict = {}
    if scope_path.exists():
        try:
            scope_data = json.loads(scope_path.read_text())
        except (json.JSONDecodeError, OSError):
            print("[risk_score] warn: could not read scope.json", flush=True)

    pr_data = fetch_pr_stats(config, pr_number)
    files = pr_data.get("files", [])
    additions = pr_data.get("additions", 0)
    deletions = pr_data.get("deletions", 0)

    blast_radius = scope_data.get("blast_radius", "medium")
    dim_size = _score_size(additions, deletions)
    dim_files = _score_file_count(len(files))
    dim_blast = _BLAST_RADIUS_SCORES.get(blast_radius, 5)
    dim_sensitive = _score_sensitive_paths(files)
    dim_test_coverage = _score_test_coverage(files)
    dim_historical = _score_historical(workspace)

    weights = {
        "size": 1.5,
        "file_count": 1.0,
        "blast_radius": 2.0,
        "sensitive_paths": 2.5,
        "test_coverage": 1.5,
        "historical": 1.0,
    }
    raw_scores = {
        "size": dim_size,
        "file_count": dim_files,
        "blast_radius": dim_blast,
        "sensitive_paths": dim_sensitive,
        "test_coverage": dim_test_coverage,
        "historical": dim_historical,
    }
    composite = sum(raw_scores[k] * weights[k] for k in raw_scores)
    tier = _tier_for_score(int(composite))
    requires_human = tier == "HIGH"

    risk_result = {
        "tier": tier,
        "score": round(composite, 1),
        "dimensions": raw_scores,
        "weights": weights,
        "requires_human_approval": requires_human,
        "scope": scope_data.get("scope", "unknown"),
        "additions": additions,
        "deletions": deletions,
        "changed_files": len(files),
    }

    out_path = workspace / "risk_score.json"
    out_path.write_text(json.dumps(risk_result, indent=2))
    print(
        f"[risk_score] tier={tier} score={composite:.1f} "
        f"requires_human={requires_human} files={len(files)} "
        f"+{additions}/-{deletions}",
        flush=True,
    )
    print(f"::add-task-context RISK_TIER::{tier}", flush=True)
    print(f"::add-task-context RISK_SCORE::{composite:.1f}", flush=True)
    print(f"::add-task-context REQUIRES_APPROVAL::{'yes' if requires_human else 'no'}", flush=True)


if __name__ == "__main__":
    main()
