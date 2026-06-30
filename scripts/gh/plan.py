"""Create implementation plan for a GitHub issue using Claude Code.

Usage:
    python -m scripts.gh.plan --issue-id 42

Required env: GH_ORG, GH_REPO
Reads:  /workspace/{issue_id}/issue.json
Writes: /workspace/{issue_id}/plan.md
        /workspace/{issue_id}/plan_result.json

Idempotent: skips if plan_result.json already shows DONE.
Exit codes: 0=done, 2=blocked, 1=error
"""

import sys
from pathlib import Path

import click

from scripts.common.artifacts import read_json, read_text, write_json, write_log, write_text
from scripts.common.claude_runner import run_claude
from scripts.common.config import get_issue_dir, load_config


PLAN_PROMPT_TEMPLATE = """\
You are an expert software engineer. Your task is to create a detailed implementation plan for the following GitHub issue.

## Issue #{issue_id}: {title}

{body}

## Instructions

1. Read CLAUDE.md, .cursorrules, .windsurfrules, or any repo-specific coding guidelines if they exist — follow them strictly.
2. Enter plan mode (type /plan) to decompose this issue into vertical-slice implementation tasks before writing any code.
3. Keep the design simple and robust: prefer modifying existing code over adding new abstractions. Avoid over-engineering.
4. Create a detailed plan in PLANS/{slug}-{issue_id}-plan.md covering:
   - Task breakdown with complexity estimates (S/M/H/XL)
   - Exact files to create/modify for each task
   - Test strategy (unit tests first, integration only if needed)
   - Any risks or blockers
5. Output ONLY this JSON on the last line (no other text after it):
   {{"status":"DONE","task_count":<N>,"total_complexity":"<S|M|H|XL>","summary":"<one sentence>"}}
   Or if blocked:
   {{"status":"BLOCKED","reason":"<explanation>"}}
"""


def _slug(title: str, max_len: int = 40) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:max_len].rstrip("-")


@click.command()
@click.option("--issue-id", required=True, help="Issue number to plan")
def main(issue_id: str) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO"])

    # Idempotency check
    existing = read_json(config, issue_id, "plan_result.json")
    if existing and existing.get("status") == "DONE":
        print(f"Plan already complete for issue #{issue_id}, skipping")
        sys.exit(0)

    issue = read_json(config, issue_id, "issue.json")
    if not issue:
        print(f"ERROR: /workspace/{issue_id}/issue.json not found", file=sys.stderr)
        sys.exit(1)

    issue_dir = get_issue_dir(config, issue_id)
    slug = _slug(issue["title"])

    prompt = PLAN_PROMPT_TEMPLATE.format(
        issue_id=issue_id,
        title=issue["title"],
        body=issue.get("body", "(no description)"),
        slug=slug,
    )

    model = config.get("AI_MODEL")
    max_turns = int(config.get("MAX_TURNS_PLAN", "50"))
    log_path = issue_dir / "logs" / "plan.log"

    print(f"Planning issue #{issue_id} with model={model}, max_turns={max_turns}")
    try:
        result = run_claude(prompt, working_dir=issue_dir, model=model, max_turns=max_turns, log_file=log_path)
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr)
        write_json(config, issue_id, "plan_result.json", {"status": "ERROR", "reason": str(e)})
        sys.exit(1)

    # Look for plan file written by claude
    plans_dir = issue_dir / "PLANS"
    plan_content = ""
    if plans_dir.exists():
        plan_files = list(plans_dir.glob("*.md"))
        if plan_files:
            plan_content = plan_files[0].read_text()

    if not plan_content:
        # Claude may have written it directly
        plan_md = issue_dir / "plan.md"
        if plan_md.exists():
            plan_content = plan_md.read_text()

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
