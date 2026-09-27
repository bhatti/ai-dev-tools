"""Run AI merge-queue analysis using the ygs-merge-queue skill via Claude.

Usage:
    python -m scripts.mq.analyze                         # reads lane_groups.json + ready_prs.json
    python -m scripts.mq.analyze --skill ygs-merge-queue # explicit skill override

Required env: ANTHROPIC_API_KEY or CLAUDE_CODE_USE_BEDROCK=1
Reads:  /workspace/lane_groups.json  (from group_by_scope)
        /workspace/ready_prs.json    (from collect_ready)
Writes: /workspace/queue_summary.json

Exit codes: 0=done, 1=error
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from scripts.common.claude_runner import SYSTEM_PROMPTS, ensure_ygs_skills, run_claude
from scripts.common.config import get_workspace_dir, load_config, validate_claude_config


def _skill_search_paths() -> list[Path]:
    paths: list[Path] = []
    codebase_dir = os.environ.get("CODEBASE_DIR", "").strip()
    if codebase_dir:
        paths.append(Path(codebase_dir) / ".claude" / "skills")
    paths.append(Path.home() / ".claude" / "skills")
    paths.append(Path.home() / ".claude" / "skills" / "you-got-skills" / "skills")
    return paths


def _load_skill_md(skill: str) -> str | None:
    for base in _skill_search_paths():
        candidate = base / skill / "SKILL.md"
        if candidate.exists():
            print(f"[mq-analyze] skill path: {candidate}", flush=True)
            return candidate.read_text(encoding="utf-8")
    return None


_ANALYZE_PROMPT = """\
Analyze the merge queue using the instructions below.

## Skill Instructions
__SKILL_INSTRUCTIONS__

## Pre-computed PR Data Schema
The following files are in /workspace — read them directly:

/workspace/ready_prs.json fields per PR:
  pr_number, repo, title, scope, blast_radius (pre-computed from diffstat),
  category (pre-computed: security/authn_authz/sre/data/api/ui/config/backend/unknown),
  category_confidence (file_path|label|title|unknown),
  pr_type (bug/feature/unknown), author, age_hours, branch, target_branch,
  ci_status (success|failed|pending|none — NOTE: BB API returns 'none' for all PRs;
             do NOT compute CI failure rates from BB data),
  has_approval, approval_count, reviewer_count,
  issue_ref ({"key":"FOO-123","url":"..."} or null),
  url, labels

/workspace/lane_groups.json fields per lane:
  lane_id ("{canonical_branch}/{risk_tier}" OR "stacked/{feature_branch}"),
  pr_count, category_counts (dict), hotspots (list of categories with >=3 bug PRs), prs

IMPORTANT: Do NOT re-derive blast_radius or category — these are pre-computed from actual
file paths via diffstat API calls. Use them directly as ground truth.
Only flag category_confidence="unknown" PRs as having uncertain classification.

Follow the skill steps exactly. Write your findings to /workspace/queue_summary.json:
{
  "status": "DONE",
  "repo": "<from ready_prs.json>",
  "total_prs": N,
  "lanes": N,
  "high_risk_prs": N,
  "needs_human_review": N,
  "conflict_lanes": N,
  "hotspots": ["category1", ...],
  "category_distribution": {"security": N, "api": N, ...},
  "by_branch": {
    "<branch>": {"prs": N, "lanes": N},
    ...
  }
}
"""

_ANALYZE_PROMPT_FALLBACK = """\
Analyze the open pull requests in /workspace/ready_prs.json and /workspace/lane_groups.json.

lane_groups.json lane_id format: "{canonical_branch}/{risk_tier}" (e.g. "main/high") for
canonical branches, "stacked/{feature_branch}" for PRs targeting feature branches.

IMPORTANT: blast_radius, category, and pr_type are pre-computed from actual file paths
and labels — do NOT re-derive them. Use them directly.

For each lane:
1. Use pre-computed blast_radius to count PRs per risk tier (low/medium/high)
2. Flag PRs where ci_status='failed' OR blast_radius='high' as needing human review
3. Check category and hotspots fields — flag any hotspot categories prominently
4. Report oldest PR (highest age_hours) in each lane
5. Group canonical lanes by target branch, count PRs and lanes per branch

Write /workspace/queue_summary.json:
{
  "status": "DONE",
  "repo": "<from ready_prs.json>",
  "total_prs": N,
  "lanes": N,
  "high_risk_prs": N,
  "needs_human_review": N,
  "conflict_lanes": 0,
  "hotspots": ["category1", ...],
  "category_distribution": {"security": N, "api": N, ...},
  "by_branch": {"<branch>": {"prs": N, "lanes": N}, ...}
}
"""


@click.command()
@click.option("--skill", default="ygs-merge-queue", show_default=True,
              help="Skill name to load for analysis")
def main(skill: str) -> None:
    config = load_config(required=[])
    validate_claude_config(config)
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)

    lane_path = workspace / "lane_groups.json"
    ready_path = workspace / "ready_prs.json"
    if not lane_path.exists() or not ready_path.exists():
        missing = [p for p in [lane_path, ready_path] if not p.exists()]
        print(f"[mq-analyze] ERROR: missing input files: {missing}", flush=True)
        sys.exit(1)

    # Quick guard: if no PRs, skip Claude and write empty summary
    ready_data = json.loads(ready_path.read_text())
    if ready_data.get("pr_count", 0) == 0:
        summary = {"status": "DONE", "repo": ready_data.get("repo", ""), "total_prs": 0,
                   "lanes": 0, "high_risk_prs": 0, "needs_human_review": 0, "conflict_lanes": 0}
        (workspace / "queue_summary.json").write_text(json.dumps(summary, indent=2))
        print("[mq-analyze] no open PRs — skipping Claude analysis", flush=True)
        print("::add-task-context TOTAL_PRS::0", flush=True)
        return

    ensure_ygs_skills()
    skill_md = _load_skill_md(skill)
    print(f"::add-task-context SKILL::{skill}", flush=True)
    print(f"::add-task-context SKILL_LOADED::{'yes' if skill_md else 'no'}", flush=True)

    if skill_md:
        prompt = _ANALYZE_PROMPT.replace("__SKILL_INSTRUCTIONS__", skill_md)
    else:
        print(f"[mq-analyze] warn: skill {skill!r} not found — using fallback prompt", flush=True)
        prompt = _ANALYZE_PROMPT_FALLBACK

    log_dir = workspace / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    try:
        run_claude(
            prompt,
            working_dir=workspace,
            model=config.get("AI_MODEL"),
            max_turns=int(config.get("MAX_TURNS_MQ_ANALYZE", "20")),
            log_file=log_dir / "analyze.log",
            allowed_tools="Bash,Read,Write,Edit,Glob,Grep,LS",
            system_prompt=SYSTEM_PROMPTS.get("code_review", SYSTEM_PROMPTS.get("standup", "")),
        )
    except RuntimeError as e:
        print(f"[mq-analyze] ERROR: Claude failed: {e}", file=sys.stderr, flush=True)
        (workspace / "queue_summary.json").write_text(
            json.dumps({"status": "ERROR", "reason": str(e)})
        )
        sys.exit(1)

    summary_path = workspace / "queue_summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())
        total = summary.get("total_prs", "?")
        lanes = summary.get("lanes", "?")
        high = summary.get("high_risk_prs", "?")
        needs_review = summary.get("needs_human_review", "?")
        print(f"[mq-analyze] done total_prs={total} lanes={lanes} high_risk={high} needs_review={needs_review}", flush=True)
        print(f"::add-task-context TOTAL_PRS::{total}", flush=True)
        print(f"::add-task-context HIGH_RISK_PRS::{high}", flush=True)
        print(f"::add-task-context NEEDS_HUMAN_REVIEW::{needs_review}", flush=True)
    else:
        print("[mq-analyze] warn: queue_summary.json not written by Claude", flush=True)


if __name__ == "__main__":
    main()
