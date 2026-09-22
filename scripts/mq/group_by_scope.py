"""Partition merge-ready PRs into independent scope lanes for parallel testing.

Usage:
    python -m scripts.mq.group_by_scope

Required env: (none — reads ready_prs.json from workspace)
Reads:  /workspace/ready_prs.json (from collect_ready)
Writes: /workspace/lane_groups.json

Exit codes: 0=done, 1=error
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import click

from scripts.common.config import get_workspace_dir, load_config


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
    print(f"[group_by_scope] grouping {len(prs)} PRs by scope", flush=True)

    scope_buckets: dict[str, list[dict]] = defaultdict(list)
    for pr in prs:
        scope = pr.get("scope", "default")
        if scope in ("cross-scope", "multi-module"):
            scope_buckets["default"].append(pr)
        else:
            scope_buckets[scope].append(pr)

    lanes = []
    for lane_id, lane_prs in sorted(scope_buckets.items()):
        lane_prs.sort(key=lambda p: p.get("age_hours", 0), reverse=True)
        lanes.append({
            "lane_id": lane_id,
            "pr_count": len(lane_prs),
            "prs": lane_prs,
        })

    lanes.sort(key=lambda l: l["pr_count"], reverse=True)

    result = {
        "lane_count": len(lanes),
        "total_prs": len(prs),
        "lanes": lanes,
    }

    out_path = workspace / "lane_groups.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"[group_by_scope] {len(lanes)} lanes: {', '.join(l['lane_id'] + '(' + str(l['pr_count']) + ')' for l in lanes)}", flush=True)

    fan_out_value = json.dumps([
        {"lane_id": l["lane_id"], "prs": json.dumps([p["pr_number"] for p in l["prs"]])}
        for l in lanes
    ])
    print(f"::add-task-context LANE_GROUPS::{fan_out_value}", flush=True)


if __name__ == "__main__":
    main()
