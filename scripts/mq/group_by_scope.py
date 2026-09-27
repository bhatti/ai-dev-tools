"""Partition merge-ready PRs into hierarchical scope lanes for risk-tiered analysis.

Lane structure:
  - Canonical branches (main/dev/stage/prod/…): lanes keyed as {branch}/{risk_tier}
    where risk_tier ∈ {high, medium, low}. Empty risk tiers are omitted.
  - Stacked PRs (PRs targeting a feature branch): lanes keyed as stacked/{feature_branch}.
    These are dependent/stacked PRs that should not be batched with the main queue.

Canonical branch detection (no hardcoding):
  1. CANONICAL_BRANCHES env var (comma-separated) — explicit overrides
  2. Default well-known names: main, master, dev, develop, stage, staging, release, prod, production
  3. Any branch targeted by >= CANONICAL_MIN_PRS (default: 3) PRs is also treated as canonical

Usage:
    python -m scripts.mq.group_by_scope

Required env: (none — reads ready_prs.json from workspace)
Reads:  /workspace/ready_prs.json (from collect_ready)
Writes: /workspace/lane_groups.json

Exit codes: 0=done, 1=error
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import click

from scripts.common.config import get_workspace_dir, load_config

def _make_lane(lane_id: str, prs: list[dict]) -> dict:
    """Build a lane dict with category distribution and hotspot detection.

    Hotspot: category has >= HOTSPOT_MIN_BUG_PRS bug-type PRs (default 3).
    See shared/merge-queue-metrics.md#pr-categories.
    """
    hotspot_threshold = int(os.environ.get("HOTSPOT_MIN_BUG_PRS", "3"))
    category_counts = dict(Counter(p.get("category", "unknown") for p in prs))
    bug_by_category = Counter(
        p.get("category", "unknown") for p in prs if p.get("pr_type") == "bug"
    )
    hotspots = [cat for cat, cnt in bug_by_category.items() if cnt >= hotspot_threshold]
    return {
        "lane_id": lane_id,
        "pr_count": len(prs),
        "category_counts": category_counts,
        "hotspots": hotspots,
        "prs": prs,
    }


_CANONICAL_DEFAULTS = frozenset({
    "main", "master", "dev", "develop", "stage", "staging",
    "release", "prod", "production",
})
_RISK_ORDER = ["high", "medium", "low"]


def _detect_canonical_branches(prs: list[dict]) -> set[str]:
    """Identify canonical target branches from env config + PR counts."""
    env_branches = {
        b.strip()
        for b in os.environ.get("CANONICAL_BRANCHES", "").split(",")
        if b.strip()
    }
    canonical = set(_CANONICAL_DEFAULTS) | env_branches

    min_prs = int(os.environ.get("CANONICAL_MIN_PRS", "3"))
    counts = Counter(pr.get("target_branch", "unknown") for pr in prs)
    for branch, count in counts.items():
        if branch and count >= min_prs:
            canonical.add(branch)
    return canonical


@click.command()
def main() -> None:
    config = load_config(required=[])
    workspace = get_workspace_dir(config)

    ready_path = workspace / "ready_prs.json"
    if not ready_path.exists():
        print("[group_by_scope] ERROR: ready_prs.json not found — run collect_ready first", file=sys.stderr)
        sys.exit(1)

    data = json.loads(ready_path.read_text())
    prs = data.get("prs", [])
    print(f"[group_by_scope] grouping {len(prs)} PRs by hierarchical scope", flush=True)

    canonical = _detect_canonical_branches(prs)

    # canonical_buckets[(branch, risk_tier)] = [prs...]
    # stacked_buckets[feature_branch] = [prs...]
    canonical_buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    stacked_buckets: dict[str, list[dict]] = defaultdict(list)

    for pr in prs:
        target = pr.get("target_branch", "") or "unknown"
        blast = pr.get("blast_radius", "low")
        risk_tier = blast if blast in _RISK_ORDER else "low"

        if target in canonical:
            canonical_buckets[(target, risk_tier)].append(pr)
        else:
            stacked_buckets[target].append(pr)

    # Count total PRs per canonical branch for sort order
    branch_pr_counts: dict[str, int] = {}
    for (branch, _), lane_prs in canonical_buckets.items():
        branch_pr_counts[branch] = branch_pr_counts.get(branch, 0) + len(lane_prs)

    lanes: list[dict] = []

    # Canonical lanes: sorted by branch PR count desc, then risk tier high→medium→low
    for (branch, risk_tier), lane_prs in sorted(
        canonical_buckets.items(),
        key=lambda x: (
            -branch_pr_counts.get(x[0][0], 0),
            x[0][0],
            _RISK_ORDER.index(x[0][1]),
        ),
    ):
        if not lane_prs:
            continue
        lane_id = f"{branch}/{risk_tier}"
        lane_prs_sorted = sorted(lane_prs, key=lambda p: p.get("age_hours", 0), reverse=True)
        lanes.append(_make_lane(lane_id, lane_prs_sorted))

    # Stacked PR lanes: sorted by PR count desc
    for feature_branch, lane_prs in sorted(stacked_buckets.items(), key=lambda x: -len(x[1])):
        lane_id = f"stacked/{feature_branch}"
        lane_prs_sorted = sorted(lane_prs, key=lambda p: p.get("age_hours", 0), reverse=True)
        lanes.append(_make_lane(lane_id, lane_prs_sorted))

    result = {
        "lane_count": len(lanes),
        "total_prs": len(prs),
        "lanes": lanes,
    }

    out_path = workspace / "lane_groups.json"
    out_path.write_text(json.dumps(result, indent=2))

    canonical_count = len(canonical_buckets)
    stacked_count = len(stacked_buckets)
    print(
        f"[group_by_scope] {len(lanes)} lanes "
        f"({canonical_count} canonical risk-tier + {stacked_count} stacked): "
        + ", ".join(f"{l['lane_id']}({l['pr_count']})" for l in lanes),
        flush=True,
    )

    fan_out_value = json.dumps([
        {"lane_id": l["lane_id"], "prs": [p["pr_number"] for p in l["prs"]]}
        for l in lanes
    ])
    print(f"::add-task-context LANE_GROUPS::{fan_out_value}", flush=True)


if __name__ == "__main__":
    main()
