"""Shared logic for issue analysis (Jira and GitHub).

Both scripts.jira.analyze_issues and scripts.gh.analyze_issues use the same
analysis prompt, Claude invocation pattern, and output artifact format.
Only the issue-fetching and issue-formatting steps differ between trackers.
"""
from __future__ import annotations

import json
import pathlib
from pathlib import Path
from typing import Optional, Tuple

from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS
from scripts.common.config import get_workspace_dir
from scripts.common.report_renderer import render_simple_html

DEFAULT_ANALYSIS_PROMPT = """\
You are a principal engineer performing evidence-based issue analysis.

For each issue, classify its type (Bug / Feature / Tech Debt / Incident) and analyze accordingly.

---

## FOR BUG ISSUES — perform all phases:

### Phase 1: Root Cause Analysis (5-Why)
- **Symptom**: What exact behavior was observed vs. expected?
- **Immediate cause**: What code/config/data caused the failure?
- **Root cause**: Why did that code exist or get merged?
- **Contributing factors**: What conditions made this harder to detect?
- **Introducing commit** (if git context is provided): identify the commit/PR that introduced the bug

### Phase 2: Process Gap Analysis
- **Spec gap**: Was the requirement or AC missing/ambiguous for this failure scenario?
- **Code review gap**: What review check would have caught this? Was complexity too high?
- **Test gap**: What unit/integration/E2E test was missing? Was only the happy path tested?
- **Observability gap**: Could better monitoring/logging have surfaced this earlier?

### Phase 3: Systems Thinking — Prevention
Across all bugs in this batch, identify:
- Recurring patterns (e.g., "3 of 5 bugs are missing null checks at API boundaries")
- High-risk components (files/modules that appear repeatedly)
- Process breakdowns (spec gaps? review gaps? test gaps?)
- Prevention levers:
  1. **Short-term**: specific PR/task to address immediate risk
  2. **Medium-term**: process or test change (e.g., new test template, review checklist)
  3. **Long-term (architectural)**: systemic change that eliminates the class of problem

---

## FOR FEATURE / TECH DEBT ISSUES:
- **Problem statement**: what user need or technical pain is this solving?
- **Proposed approach**: concrete implementation suggestions
- **Risks**: what could go wrong?
- **Dependencies**: what needs to be done first?

---

## FOR ALL ISSUES:
- **Priority**: P0 Critical / P1 High / P2 Medium / P3 Low with justification
- **Effort estimate**: XS (<1d) / S (1-2d) / M (3-5d) / L (1-2w) / XL (>2w)

Be concise. Use bullet points. Focus on actionable guidance. Cite issue keys and commit hashes.

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


def resolve_skill_for_analyze(
    prompt: Optional[str],
    query: Optional[str],
    issues_text: str,
    config: dict,
    log_prefix: str = "[analyze]",
) -> Optional[Tuple[str, Path]]:
    """Find the best skill for the analyze workflow.

    Tries ygs-analyze directly first, then falls back to keyword-based matching.
    Extracted from both analyze scripts — they were byte-for-byte identical.
    """
    from scripts.common.skill_resolver import find_skill, find_skill_for_query
    direct = find_skill("ygs-analyze", config)
    if direct:
        print(f"{log_prefix} found ygs-analyze skill directly at {direct}", flush=True)
        return ("ygs-analyze", direct)
    return find_skill_for_query(prompt or query or issues_text[:200], config)


def try_git_archaeology(
    config: dict,
    issue_ids: list[str],
    tracker: str = "",
) -> Tuple[Optional[str], Optional[Path]]:
    """Clone repo via clone_by_tracker() and run git archaeology.

    Returns (context_markdown, repo_path) or (None, None) on failure/skipped.
    Emits ::add-task-context markers for CLONE_METHOD, REPO_DETECTED, REPO_ORG,
    REPO_NAME, and CLONE_SKIP_REASON so test assertions and Formicary task context
    all reflect what actually happened.

    tracker: "jira"|"bitbucket" or "github" — passed explicitly by each caller
             so this function never infers it from DEFAULT_TRACKER.
    """
    from scripts.common.git_archaeology import (
        build_context as _git_build_context,
    )
    from scripts.common.git_utils import clone_by_tracker
    from scripts.common.issue_fetcher import detect_repo_from_issue

    # Normalise tracker string
    tracker_norm = tracker.lower().strip()
    is_jira_bb = tracker_norm in ("jira", "bitbucket", "jira/bitbucket")

    # Determine whether a repo is configured via env/org-config
    if is_jira_bb:
        has_repo = bool(config.get("BITBUCKET_WORKSPACE") and config.get("BITBUCKET_REPO"))
    else:
        has_repo = bool(config.get("GH_ORG") and config.get("GH_REPO"))

    repo_detected_from = "env" if has_repo else "none"

    # If repo not in config, try to detect from issue body
    if not has_repo:
        print(f"[analyze] no repo configured in env — checking issue body for repo URL ...",
              flush=True)
        # issue_ids is a list of issue keys/numbers; we can't pass issue data here
        # so detection is deferred to the caller via _patch_config_from_issue_body
        print(f"::add-task-context REPO_DETECTED::none", flush=True)
        print(f"::add-task-context CLONE_SKIP_REASON::no_repo_configured", flush=True)
        return None, None

    # Determine repo label for context markers
    if is_jira_bb:
        repo_org = config.get("BITBUCKET_WORKSPACE", "")
        repo_name = config.get("BITBUCKET_REPO", "")
    else:
        repo_org = config.get("GH_ORG", "")
        repo_name = config.get("GH_REPO", "")

    dest = Path(config.get("WORKSPACE_DIR", "/tmp")) / "repo_cache"
    try:
        print(f"[analyze] cloning {repo_org}/{repo_name} for git archaeology "
              f"(tracker={tracker_norm}) ...", flush=True)
        repo_path = clone_by_tracker(config, dest, tracker=tracker_norm)
        # Mirror clone_by_tracker's auth decision to emit the correct CLONE_METHOD marker
        if is_jira_bb:
            http_token = config.get("BITBUCKET_TOKEN", config.get("BITBUCKET_APP_PASSWORD", ""))
            clone_method = "https" if http_token else "ssh"
        else:
            token = config.get("GH_TOKEN", "")
            use_ssh = not token or config.get("USE_SSH", "0") == "1"
            clone_method = "ssh" if use_ssh else "https"

        print(f"::add-task-context CLONE_METHOD::{clone_method}", flush=True)
        print(f"::add-task-context REPO_DETECTED::{repo_detected_from}", flush=True)
        print(f"::add-task-context REPO_ORG::{repo_org}", flush=True)
        print(f"::add-task-context REPO_NAME::{repo_name}", flush=True)

        # Format keys for git log grep: Jira uses "PROJ-123", GH uses "#123"
        if is_jira_bb:
            grep_keys = issue_ids
        else:
            grep_keys = [f"#{i.lstrip('#')}" for i in issue_ids]

        print(f"[analyze] running git archaeology on {repo_path}", flush=True)
        context = _git_build_context(repo_path, grep_keys) or None
        return context, repo_path
    except Exception as e:
        print(f"[analyze] WARNING: git archaeology failed: {e} — continuing without git context",
              flush=True)
        print(f"::add-task-context CLONE_METHOD::failed", flush=True)
        print(f"::add-task-context REPO_DETECTED::{repo_detected_from}", flush=True)
        print(f"::add-task-context REPO_ORG::{repo_org}", flush=True)
        print(f"::add-task-context REPO_NAME::{repo_name}", flush=True)
        print(f"::add-task-context CLONE_SKIP_REASON::clone_failed", flush=True)
        return None, None


def emit_git_context_markers(
    config: dict,
    git_context: Optional[str],
    git_repo_path: Optional[Path],
    tracker: str,
) -> str:
    """Emit GIT_* task context markers and return the git header line for the Slack message.

    Replaces the verbatim 25-line block duplicated in both analyze scripts.
    Returns empty string when git_context is None.
    """
    from scripts.common.git_archaeology import (
        extract_stats as _extract_stats,
        get_repo_info as _get_repo_info,
    )
    if not git_context:
        return ""

    tracker_norm = tracker.lower().strip()
    is_jira_bb = tracker_norm in ("jira", "bitbucket", "jira/bitbucket")

    if is_jira_bb:
        org = config.get("BITBUCKET_WORKSPACE", "")
        repo = config.get("BITBUCKET_REPO", "")
    else:
        org = config.get("GH_ORG", "")
        repo = config.get("GH_REPO", "")

    repo_label = f"{org}/{repo}" if org and repo else repo or org
    stats = _extract_stats(git_context)

    print(f"::add-task-context GIT_REPO::{repo_label}", flush=True)
    print(f"::add-task-context GIT_COMMITS_FOUND::{stats['commits_found']}", flush=True)
    print(f"::add-task-context GIT_HOT_FILES::{stats['hot_files']}", flush=True)

    if git_repo_path:
        repo_info = _get_repo_info(git_repo_path)
        if repo_info.get("branch"):
            print(f"::add-task-context GIT_BRANCH::{repo_info['branch']}", flush=True)
        if repo_info.get("head_commit"):
            print(f"::add-task-context GIT_HEAD_COMMIT::{repo_info['head_commit']}", flush=True)
        if repo_info.get("head_author"):
            print(f"::add-task-context GIT_HEAD_AUTHOR::{repo_info['head_author']}", flush=True)
        if repo_info.get("head_date"):
            print(f"::add-task-context GIT_HEAD_DATE::{repo_info['head_date']}", flush=True)

    parts = [f"cloned `{repo_label}`"] if repo_label else []
    if stats["commits_found"]:
        parts.append(f"{stats['commits_found']} related commits")
    if stats["top_hot_file"]:
        parts.append(f"hottest: `{stats['top_hot_file']}`")
    return f"📂 *Git context:* {', '.join(parts)}\n" if parts else ""


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
