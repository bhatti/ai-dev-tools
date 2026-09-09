"""Plan skill updates based on PR audit findings.

Runs a focused Claude Code session that reads the audit findings and refines
skill improvement proposals into a detailed plan. Mirrors the pattern of
scripts/gh/plan.py (same claude_runner.run_claude + SYSTEM_PROMPTS["plan"]).

Usage:
    python -m scripts.analyze.plan_skill_updates

Reads:
    /workspace/reports/pr_audit_report.md     (required)
    /workspace/reports/skill_improvements.json (optional)

Writes:
    /workspace/reports/skill_update_plan.md
    /workspace/reports/skill_update_plan_result.json

Exit codes: 0=done, 1=error
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.common.artifacts import write_json
from scripts.common.claude_runner import SYSTEM_PROMPTS, run_claude
from scripts.common.config import get_workspace_dir, load_config, validate_claude_config
from scripts.common.idempotency import check_done

_MAX_FINDINGS_CHARS = 3000
_MAX_IMPROVEMENTS_CHARS = 2000

_PLAN_PROMPT_TEMPLATE = """\
You are a skill-improvement planner for the {org}/{repo} codebase.

An automated PR audit has identified gaps in skills, spec, testing, and design patterns.
Your job is to review the findings and create a detailed, actionable skill update plan.

## Audit Findings (excerpt)

{findings_excerpt}

## Proposed Changes from Audit

{improvements_summary}

## Instructions

1. Read `reports/pr_audit_report.md` for the full audit findings.
2. Read `reports/skill_improvements.json` for the complete proposed changes list.
3. For each entry in `repo_skill_changes`: if the target file exists under `.claude/skills/`,
   read it to understand what is already there vs. what needs to change.
4. For each entry in `new_docs`: check whether an equivalent file already exists in the repo.
5. Review and refine the proposals:
   - Expand vague descriptions into specific, actionable content
   - Correct file paths (must be relative to repo root)
   - Combine duplicates or near-duplicates into a single entry
   - Remove any entries that are already covered by existing skills
   - **REJECT any proposed change that introduces generic process rules** (LOC-based thresholds,
     mandatory reviewer counts, blanket "design doc required" policies, etc.) unless the audit
     report cites multiple specific PRs where the absence of that practice caused a real defect.
     The team has millions of LOC and thousands of engineers — process overhead must be justified
     by observed recurring problems, not textbook best practices.
6. Identify gaps: anything the audit report flags that `skill_improvements.json` does not address.
7. Write your detailed plan to `reports/skill_update_plan.md` with:
   - **Executive summary**: which SPECIFIC recurring gaps are addressed and the PRs that evidence them
   - **Per-change entry**: file path, gap addressed, exact change/content to write, PRs that motivated it
   - **Priority order**: highest-impact changes first (most frequently recurring gap → highest priority)
8. Output ONLY this JSON on the last line (no text after it):
   {{"status":"DONE","skill_updates":<N>,"new_skills":<M>,"summary":"<one sentence>"}}
   Or if blocked:
   {{"status":"BLOCKED","reason":"<explanation>"}}
"""


def main() -> None:
    config = load_config(required=[])
    validate_claude_config(config)

    workspace_dir = get_workspace_dir(config)
    reports_dir = workspace_dir / "reports"
    logs_dir = workspace_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # Idempotency: skip if already DONE (same pattern as plan.py / check_done)
    check_done(reports_dir / "skill_update_plan_result.json")

    # Read audit report (required)
    audit_report_path = reports_dir / "pr_audit_report.md"
    if not audit_report_path.exists():
        print(f"ERROR: {audit_report_path} not found — run audit-prs first", file=sys.stderr)
        sys.exit(1)
    findings_text = audit_report_path.read_text(encoding="utf-8")
    findings_excerpt = findings_text[:_MAX_FINDINGS_CHARS]
    if len(findings_text) > _MAX_FINDINGS_CHARS:
        findings_excerpt += "\n\n... (see reports/pr_audit_report.md for full content)"

    # Read skill improvements (optional)
    improvements_path = reports_dir / "skill_improvements.json"
    if improvements_path.exists():
        try:
            improvements_data = json.loads(improvements_path.read_text(encoding="utf-8"))
            improvements_summary = json.dumps(improvements_data, indent=2)[:_MAX_IMPROVEMENTS_CHARS]
        except (json.JSONDecodeError, OSError) as e:
            print(f"[plan-skill-updates] WARNING: could not read skill_improvements.json: {e}", flush=True)
            improvements_summary = "(not available)"
    else:
        improvements_summary = "(not available — audit produced no skill_improvements.json)"

    tracker = (config.get("DEFAULT_TRACKER") or "").lower().strip()
    if tracker in ("jira", "bitbucket", "jira/bitbucket"):
        org = config.get("BITBUCKET_WORKSPACE", "unknown")
        repo = config.get("BITBUCKET_REPO", "unknown")
    else:
        org = config.get("GH_ORG", "unknown")
        repo = config.get("GH_REPO", "unknown")
    prompt = _PLAN_PROMPT_TEMPLATE.format(
        org=org,
        repo=repo,
        findings_excerpt=findings_excerpt,
        improvements_summary=improvements_summary,
    )

    model = config.get("AI_MODEL", config.get("ANTHROPIC_DEFAULT_SONNET_MODEL", ""))
    max_turns = int(config.get("MAX_TURNS_PLAN", "30"))
    print(f"[plan-skill-updates] org={org} repo={repo} model={model} max_turns={max_turns}", flush=True)

    try:
        result = run_claude(
            prompt,
            working_dir=workspace_dir,
            model=model,
            max_turns=max_turns,
            log_file=logs_dir / "plan_skill_updates.log",
            system_prompt=SYSTEM_PROMPTS["plan"],
        )
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr)
        write_json(config, "pr-audit", "reports/skill_update_plan_result.json",
                   {"status": "ERROR", "reason": str(e)})
        sys.exit(1)

    status_data = result.status_json or {"status": result.status}
    # Use write_json (same as plan.py / push_impl.py — DRY with implement workflow)
    write_json(config, "pr-audit", "reports/skill_update_plan_result.json", status_data)

    plan_path = reports_dir / "skill_update_plan.md"
    if plan_path.exists():
        print(f"[plan-skill-updates] skill_update_plan.md written ({plan_path.stat().st_size} bytes)",
              flush=True)
        print(f"::add-task-context SKILL_UPDATE_PLAN::yes", flush=True)
    else:
        print("[plan-skill-updates] WARNING: skill_update_plan.md was not written by Claude", flush=True)
        print(f"::add-task-context SKILL_UPDATE_PLAN::no", flush=True)

    status = status_data.get("status", "UNKNOWN")
    print(f"[plan-skill-updates] status={status} result={json.dumps(status_data)}", flush=True)

    if status == "ERROR":
        sys.exit(1)


if __name__ == "__main__":
    main()
