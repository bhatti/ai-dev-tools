"""Shared logic for issue analysis (Jira and GitHub).

Both scripts.jira.analyze_issues and scripts.gh.analyze_issues use the same
analysis prompt, Claude invocation pattern, and output artifact format.
Only the issue-fetching and issue-formatting steps differ between trackers.
"""
from __future__ import annotations

import json
import pathlib

from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS
from scripts.common.config import get_workspace_dir
from scripts.common.report_renderer import render_simple_html

DEFAULT_ANALYSIS_PROMPT = """\
You are a senior software engineer. Analyze the following issues and provide:

1. **Root cause summary** — what is the common theme or underlying cause?
2. **Possible fixes** — concrete, actionable suggestions for each issue (or a batch fix if they share a root cause).
3. **Priority recommendation** — which issues to fix first and why.
4. **Effort estimate** — S/M/H/XL per issue or batch.

Be concise. Use bullet points. Focus on actionable guidance.

## Issues

{issues_text}
"""


def run_skill_analysis(config: dict, issues_text: str, skill_name: str, skill_path,
                       git_context: str | None = None) -> str:
    """Invoke a skill's SKILL.md instructions for analysis via Claude. DRY shared version."""
    workspace = pathlib.Path(config.get("WORKSPACE_DIR", "/tmp"))
    skill_md = pathlib.Path(skill_path).read_text(encoding="utf-8")
    prompt = f"{skill_md}\n\n## Issue Context to Analyze\n\n{issues_text}"
    if git_context:
        prompt += f"\n\n## Git Repository Context\n\n{git_context}"
    result = run_claude(
        prompt,
        working_dir=workspace,
        model=config.get("AI_MODEL"),
        max_turns=20,
        log_file=workspace / "logs" / "analyze.log",
        allowed_tools="Bash,Read,Write,Edit,Glob,Grep,LS",
        system_prompt=SYSTEM_PROMPTS["plan"],
    )
    return result.output.strip()


def run_analysis(config: dict, issues_text: str, git_context: str | None = None) -> str:
    """Run Claude on pre-formatted issues text; return the analysis string."""
    workspace = pathlib.Path(config.get("WORKSPACE_DIR", "/tmp"))
    log_dir = workspace / "logs"
    prompt_template = config.get("ANALYSIS_PROMPT") or DEFAULT_ANALYSIS_PROMPT
    prompt = prompt_template.format(issues_text=issues_text)
    if git_context:
        prompt += f"\n\n{git_context}"
    result = run_claude(
        prompt,
        working_dir=workspace,
        model=config.get("AI_MODEL"),
        max_turns=5,
        log_file=log_dir / "analyze.log",
        allowed_tools=None,
        system_prompt=SYSTEM_PROMPTS["plan"],
    )
    return result.output.strip()


def write_analysis_output(config: dict, issue_ids: list[str], analysis: str) -> None:
    """Write reports/result.json, reports/report.md, reports/report.html."""
    workspace = get_workspace_dir(config)
    reports = workspace / "reports"
    reports.mkdir(parents=True, exist_ok=True)

    result = {"count": len(issue_ids), "keys": issue_ids, "analysis": analysis}
    (reports / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    header = f"# Analysis of {len(issue_ids)} issue(s): {', '.join(issue_ids)}\n\n"
    md_text = header + analysis
    (reports / "report.md").write_text(md_text, encoding="utf-8")

    title = f"Analysis of {len(issue_ids)} issue(s)"
    (reports / "report.html").write_text(render_simple_html(title, md_text), encoding="utf-8")
    print("[analyze] wrote reports/result.json, reports/report.md, reports/report.html", flush=True)
