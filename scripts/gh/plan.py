"""Create implementation plan for a GitHub issue using Claude Code.

Usage:
    python -m scripts.gh.plan --issue-id 42

Required env: GH_ORG, GH_REPO
Reads:  /workspace/issue.json
Writes: /workspace/plan.md
        /workspace/plan_result.json

Idempotent: skips if plan_result.json already shows DONE.
Exit codes: 0=done, 2=blocked, 1=error
"""

import sys

import click

from scripts.common.artifacts import find_plan_content, read_json, write_json, write_text
from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS
from scripts.common.config import get_issue_dir, load_config, validate_claude_config
from scripts.common.idempotency import check_done
from scripts.common.text_utils import slug


PLAN_PROMPT_TEMPLATE = """\
You are an expert software engineer creating an implementation plan for a GitHub issue.

## Issue #{issue_id}: {title}

{body}

## Instructions

1. Read CLAUDE.md, .cursorrules, .windsurfrules, or any repo-specific coding guidelines if they exist — follow them strictly. Treat them as the authoritative source for standards, constraints, and process.
2. Discover `.claude/skills/` in the repo. If a skill applies to this work, plan to invoke it rather than reimplementing its steps.
3. Before designing new abstractions, search `utils/`, `shared/`, `common/` for existing utilities related to this issue. List any reuse candidates in the plan under "Reuse Candidates".
4. Check for monorepo structure (workspace files, directory layout) and pin exploration to the relevant packages only.
5. Generate a concise plan covering:
   - Task breakdown with complexity estimates (S/M/H/XL)
   - Exact files to create/modify per task
   - Test strategy: write failing tests first, then implement
   - A "Failing Test Spec" section: inputs, expected outputs, assertions for the desired behavior
   - Any risks or blockers
6. Classify overall complexity using this rubric:
   - **S/low**: ≤3 files, no new abstractions, existing tests cover the area
   - **M/medium**: 4-10 files OR new function/class OR config schema change
   - **H/high**: >10 files OR new module OR API contract change
   When uncertain between two levels, round up.
7. Write the plan to PLANS/{slug}-{issue_id}-plan.md.
8. If you cannot make a plan (requirements unclear, insufficient context): explain why — do NOT modify any other files.
9. Output ONLY this JSON on the last line (no text after it):
   {{"status":"DONE","task_count":<N>,"total_complexity":"<S|M|H|XL>","summary":"<one sentence>"}}
   Or if blocked / requirements unclear:
   {{"status":"BLOCKED","reason":"<explanation>"}}
"""


@click.command()
@click.option("--issue-id", required=True, help="Issue number to plan")
def main(issue_id: str) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO"])
    validate_claude_config(config)
    print(f"[plan] issue={issue_id} org={config['GH_ORG']} repo={config['GH_REPO']}", flush=True)

    issue_dir = get_issue_dir(config, issue_id)
    check_done(issue_dir / "plan_result.json")

    issue = read_json(config, issue_id, "issue.json")
    if not issue:
        print(f"ERROR: {issue_dir}/issue.json not found", file=sys.stderr)
        sys.exit(1)

    issue_slug = slug(issue["title"])
    prompt = PLAN_PROMPT_TEMPLATE.format(
        issue_id=issue_id,
        title=issue["title"],
        body=issue.get("body", "(no description)"),
        slug=issue_slug,
    )

    model = config.get("AI_MODEL")
    max_turns = int(config.get("MAX_TURNS_PLAN", "50"))
    print(f"[plan] model={model} max_turns={max_turns}", flush=True)

    try:
        result = run_claude(prompt, working_dir=issue_dir, model=model, max_turns=max_turns,
                            log_file=issue_dir / "logs" / "plan.log",
                            system_prompt=SYSTEM_PROMPTS["plan"])
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr)
        write_json(config, issue_id, "plan_result.json", {"status": "ERROR", "reason": str(e)})
        sys.exit(1)

    plan_content = find_plan_content(issue_dir)
    if plan_content:
        write_text(config, issue_id, "plan.md", plan_content)

    write_json(config, issue_id, "plan_result.json", result.status_json or {"status": result.status})

    if result.status == "BLOCKED":
        print(f"Plan blocked: {result.status_json.get('reason', 'unknown')}")
        sys.exit(2)

    if result.status not in ("DONE",):
        print(f"Warning: unexpected plan status '{result.status}'")

    print(f"Plan complete: {result.status_json.get('summary', '')}")
    sys.exit(0)


if __name__ == "__main__":
    main()
