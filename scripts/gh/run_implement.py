"""Run Claude to implement a GitHub issue in the cloned repo.

Usage:
    python -m scripts.gh.run_implement --issue-id 42

Reads:  /workspace/{issue_id}/issue.json
        /workspace/{issue_id}/plan.md
        /workspace/{issue_id}/branch.txt
Writes: /workspace/{issue_id}/impl_run_result.json
        /workspace/{issue_id}/impl_report.md
        /workspace/{issue_id}/impl_report.html
        /workspace/{issue_id}/logs/implement.log

Exit codes: 0=done/partial, 1=error
"""

import sys

import click

from scripts.common.artifacts import read_json, read_text, write_json, write_log
from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS
from scripts.common.config import get_issue_dir, load_config, validate_claude_config
from scripts.common.idempotency import check_done
from scripts.common.report_renderer import (
    render_implement_report_md,
    render_implement_report_html,
)


IMPLEMENT_PROMPT_TEMPLATE = """\
You are an expert software engineer implementing a GitHub issue.

## Issue #{issue_id}: {title}

{body}

## Implementation Plan

{plan}

## Instructions

IMPORTANT: You have a limited number of turns. Start implementing immediately — trust the plan above which already identifies the files and approach. Do not spend turns broadly exploring the repo.

1. Read CLAUDE.md, .cursorrules, .windsurfrules, or any repo-specific coding guidelines if they exist (one read, then proceed). Never deviate from the existing language, style, and patterns.
2. Trust the plan — go straight to editing the specific files it identifies. Use `grep` to find exact locations when needed, not broad exploration.
3. Before writing any implementation code, check `.claude/skills/` for relevant skills. If the plan names a skill, invoke it via the Skill tool rather than reimplementing its steps.
4. For each plan task:
   - Make targeted file changes following the repo's existing patterns.
   - Do NOT modify files unrelated to the task.
   - Run only the tests covering the changed code (not the full suite per task).
   - Commit with message: "task: <description>"
5. After ALL tasks are done, run the full test suite once. Do NOT run lint or eslint.
6. If tests fail, fix them immediately. Stop after 2 consecutive failed fix attempts.
7. After tests pass, do a cleanup pass: remove any unused variables, imports, or dead code you introduced.
8. Self-review your diff (max 2 cycles — fix issues found, then re-check once):
   - Verify each acceptance criterion in the issue is satisfied
   - Check for debug code, TODOs, hardcoded values, unused imports/variables
   - Check failure modes: partial failure, concurrent access, large/empty inputs
   - Verify no scope creep beyond the issue description
   - Check naming consistency with surrounding code
   - If issues found: fix them. After 2 cycles, flag remaining concerns in `notes`.
9. Output ONLY this JSON on the last line (no text after it):
   {{"status":"DONE","files_changed":["file1","file2"],"commits":<N>,"tests_status":"passing","summary":"<one sentence: what was implemented>","notes":"<any caveats or unresolved concerns, or empty string>"}}
   Or if blocked / requirements unclear:
   {{"status":"CANNOT_IMPLEMENT","reason":"<explanation>","files_changed":[],"commits":0,"notes":""}}
   Or if tests still failing after retries:
   {{"status":"TESTS_FAILING","reason":"<explanation>","files_changed":["file1"],"commits":<N>,"notes":""}}

IMPORTANT: Always write the JSON result on the last line regardless of outcome. This is the handoff contract for the next step.
"""


@click.command()
@click.option("--issue-id", required=True, help="GitHub issue number")
def main(issue_id: str) -> None:
    config = load_config(required=[])
    validate_claude_config(config)
    print(f"[run-implement] issue={issue_id}", flush=True)

    issue_dir = get_issue_dir(config, issue_id)
    check_done(issue_dir / "impl_run_result.json")

    issue = read_json(config, issue_id, "issue.json")
    if not issue:
        print(f"ERROR: {issue_dir}/issue.json not found", file=sys.stderr)
        sys.exit(1)

    plan = read_text(config, issue_id, "plan.md")
    if not plan:
        print(f"ERROR: {issue_dir}/plan.md not found", file=sys.stderr)
        sys.exit(1)

    branch_file = issue_dir / "branch.txt"
    if not branch_file.exists():
        print(f"ERROR: {branch_file} not found — run clone-repo first", file=sys.stderr)
        sys.exit(1)

    repo_dir = issue_dir / "repo"
    prompt = IMPLEMENT_PROMPT_TEMPLATE.format(
        issue_id=issue_id,
        title=issue["title"],
        body=issue.get("body", "(no description)"),
        plan=plan,
    )

    model = config.get("AI_MODEL")
    max_turns = int(config.get("MAX_TURNS_IMPLEMENT", "100"))
    print(f"[run-implement] model={model} max_turns={max_turns}", flush=True)

    try:
        result = run_claude(
            prompt,
            working_dir=repo_dir,
            model=model,
            max_turns=max_turns,
            log_file=issue_dir / "logs" / "implement.log",
            system_prompt=SYSTEM_PROMPTS["implement"],
        )
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr)
        error_data = {"status": "ERROR", "reason": str(e), "files_changed": [], "commits": 0}
        write_json(config, issue_id, "impl_run_result.json", error_data)
        _write_impl_reports(issue_dir, issue_id, error_data)
        sys.exit(1)

    result_data = result.status_json or {"status": result.status}
    write_json(config, issue_id, "impl_run_result.json", result_data)
    write_log(config, issue_id, "implement", result.output)
    print(f"[run-implement] status={result.status}", flush=True)

    _write_impl_reports(issue_dir, issue_id, result_data)
    sys.exit(0)


def _write_impl_reports(issue_dir, issue_id: str, result_data: dict) -> None:
    """Always render human-readable self-review reports as artifacts."""
    md_text = render_implement_report_md(issue_id, result_data)
    html_text = render_implement_report_html(issue_id, result_data, md_text)
    (issue_dir / "impl_report.md").write_text(md_text, encoding="utf-8")
    (issue_dir / "impl_report.html").write_text(html_text, encoding="utf-8")
    print(f"[run-implement] wrote impl_report.md + impl_report.html", flush=True)


if __name__ == "__main__":
    main()
