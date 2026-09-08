"""Run ygs-codebase-audit on a git repository using Claude Code.

Usage:
    python -m scripts.analyze.run_codebase_audit [--repo-url <url>] [--branch main]
        [--commits 1000] [--focus all]

Required env: ANTHROPIC_API_KEY or CLAUDE_CODE_USE_BEDROCK=1

Repo URL resolution (in priority order):
  1. --repo-url flag
  2. CODEBASE_REPO_URL env
  3. DEFAULT_TRACKER=jira/bitbucket → BITBUCKET_WORKSPACE + BITBUCKET_REPO
  4. DEFAULT_TRACKER=github → GH_ORG + GH_REPO

Writes:
  /workspace/reports/audit_findings.json
  /workspace/reports/audit_report.md
  /workspace/reports/audit_report.html
  /workspace/logs/audit.log

Exit codes: 0=done, 1=error
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import click

from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS, _ensure_ygs_skills
from scripts.common.config import get_workspace_dir, load_config, validate_claude_config
from scripts.common.git_archaeology import build_audit_context, get_repo_info
from scripts.common.repo_utils import resolve_repo_url, repo_label as compute_repo_label, clone_for_audit
from scripts.common.report_renderer import render_simple_html
from scripts.common.skills import apply_project_skills, inline_shared_refs


# ── Skill discovery ────────────────────────────────────────────────────────────

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
            print(f"[audit] skill path: {candidate}", flush=True)
            content = candidate.read_text(encoding="utf-8")
            return inline_shared_refs(content)
    return None


def _load_dimension_skill(
    primary: str,
    fallback_names: list[str],
    config: dict,
) -> str | None:
    """Load a dimension-specific skill, trying repo-local first then ygs base skills.

    Returns the skill content (inlined shared refs), or None if nothing found.
    """
    for candidate in [primary] + fallback_names:
        content = _load_skill_md(candidate)
        if content:
            print(f"[audit] dimension skill: {candidate}", flush=True)
            return content
    return None


# ── Clone ──────────────────────────────────────────────────────────────────────

def _parse_slack_flags(config: dict) -> tuple[int | None, int | None]:
    """Parse inline Slack flags from SLACK_MESSAGE env var.

    Supports natural language like:
      -- audit for last 200 commits, max size 2MB
      -- audit for last 50 commits
      -- audit with max size 2MB
      --commits 200
      --max-size 2097152

    Returns (n_commits_override, max_code_size_override) — None if not found.
    """
    msg = config.get("SLACK_MESSAGE", os.environ.get("SLACK_MESSAGE", "")).lower()
    if not msg:
        return None, None

    n_commits: int | None = None
    max_size: int | None = None

    m = re.search(r"(?:last\s+)?(\d+)\s+commits?|--commits\s+(\d+)", msg)
    if m:
        val = m.group(1) or m.group(2)
        if val:
            n_commits = int(val)

    m = re.search(r"max\s*size\s+(\d+(?:\.\d+)?)\s*(mb|kb|gb|b)?|--max-size\s+(\d+)", msg)
    if m:
        if m.group(3):
            max_size = int(m.group(3))
        else:
            val = float(m.group(1))
            unit = (m.group(2) or "mb").lower()
            if unit == "kb":
                max_size = int(val * 1024)
            elif unit == "gb":
                max_size = int(val * 1024 * 1024 * 1024)
            elif unit == "b":
                max_size = int(val)
            else:
                max_size = int(val * 1024 * 1024)

    return n_commits, max_size



# ── Markers ────────────────────────────────────────────────────────────────────

def _emit_finding_counts(findings_path: Path, fallback_repo: str = "", fallback_branch: str = "") -> None:
    """Parse audit_findings.json and emit ::add-task-context markers."""
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
        crit = data.get("critical_count", 0)
        high = data.get("high_count", 0)
        # Use fallback values when Claude omits or blanks these fields
        repo = data.get("repo") or fallback_repo
        branch = data.get("branch") or fallback_branch
        commits = data.get("commits_analyzed", 0)
        focus = data.get("focus", "all")
        hotspot = ""
        for f in data.get("findings", []):
            if f.get("dimension") in ("hotspot", "architecture", "security"):
                loc = f.get("location", "")
                if loc:
                    hotspot = loc
                    break
        if repo:
            print(f"::add-task-context AUDIT_REPO::{repo}", flush=True)
        if branch:
            print(f"::add-task-context AUDIT_BRANCH::{branch}", flush=True)
        if commits:
            print(f"::add-task-context AUDIT_COMMITS::{commits}", flush=True)
        print(f"::add-task-context AUDIT_FOCUS::{focus}", flush=True)
        print(f"::add-task-context AUDIT_CRITICAL_COUNT::{crit}", flush=True)
        print(f"::add-task-context AUDIT_HIGH_COUNT::{high}", flush=True)
        if hotspot:
            print(f"::add-task-context AUDIT_HOTSPOT_FILE::{hotspot}", flush=True)
    except Exception as e:
        print(f"[audit] could not parse findings for markers: {e}", flush=True)


def _write_audit_reports(workspace: Path, stub: dict) -> None:
    """Ensure reports/audit_findings.json and reports/audit_report.md exist."""
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    findings_path = reports_dir / "audit_findings.json"
    if not findings_path.exists():
        findings_path.write_text(json.dumps(stub, indent=2), encoding="utf-8")
        print("[audit] wrote stub audit_findings.json", flush=True)

    report_path = reports_dir / "audit_report.md"
    if not report_path.exists():
        report_path.write_text(
            f"# Codebase Audit — {stub.get('repo', 'unknown')}\n\n"
            "Audit did not complete — see audit.log for details.\n",
            encoding="utf-8",
        )
        print("[audit] wrote stub audit_report.md", flush=True)


# ── Fallback prompt ────────────────────────────────────────────────────────────

_FALLBACK_PROMPT = """\
You are a principal engineer performing codebase health analysis.

Analyze the repository in the current working directory for:
1. Hotspot files — top files by change frequency and risk
2. Duplicate abstractions — same utility names in different modules
3. Architecture drift — cross-boundary imports, large files (>2000 lines)
4. Test health — production files changed frequently with no test co-changes
5. Commit quality — fix: ratio, large commits, vague messages

Write findings to:
- reports/audit_findings.json (see format in instructions)
- reports/audit_report.md (markdown report with executive summary and findings tables)

DO NOT emit any ::add-task-context markers — the orchestrator reads your JSON and emits them.

Output ONLY this JSON on the last line:
{"status":"DONE","critical_count":<N>,"high_count":<N>,"summary":"<one sentence>"}
Or on failure:
{"status":"ERROR","reason":"<explanation>"}
"""

_AUDIT_PROMPT_TEMPLATE = """\
## Codebase Audit Task

**Repository**: {repo_label}
**Branch**: {branch}
**Commits analyzed**: {n_commits}{commit_range_line}
**Focus**: {focus}
**Working directory**: You are in the cloned repository root — run `grep`, `find`, `cat`, etc. directly on the source files.
**Reports directory**: Write output files to `./reports/` (relative path, same as other workflows).

## Audit Instructions

{skill_instructions}

{dimension_skills_block}

## Repository Analysis Data

The following statistics have been pre-computed from git history.
Use them as *hints* — do NOT report findings from statistics alone.
Verify every finding by running a Bash command and showing actual output.

{audit_context}

## REQUIRED OUTPUTS — Must be written before emitting the final JSON line

Write these files using relative paths from the repo root (the `reports/` symlink resolves to the workspace reports directory):

1. `reports/audit_report.md` — Comprehensive markdown audit report:
   - Executive summary (2-3 sentences: most critical risk, fix: ratio, top hotspot)
   - CRITICAL findings section with file:line evidence and Bash command output
   - HIGH findings section (same format)
   - MEDIUM findings section
   - "Checked — No Issues Found" section for clean dimensions
   - Metrics Dashboard table
   - Minimum 1000 chars. If you write less than 1000 chars, you did not do the job.

2. `reports/audit_findings.json` — Structured JSON:
   {{"repo":"{repo_label}","branch":"{branch}","commits_analyzed":{n_commits},"focus":"{focus}",
    "critical_count":N,"high_count":N,
    "findings":[{{"severity":"CRITICAL|HIGH|MEDIUM|LOW","dimension":"hotspot|architecture|security|sre|tests|duplicates|knowledge-silo|commit-quality","location":"path/to/file:line or module","evidence":"command → output snippet","recommendation":"specific action"}}],
    "metrics":{{"fix_ratio":0.0,"avg_files_per_commit":0.0,"single_author_hotspots":0,"temporal_coupling_pairs":0,"test_gap_files":0}}}}

DO NOT emit any ::add-task-context markers yourself — the orchestrator script reads
your JSON output and emits them automatically. Focus only on writing the two report files.

---

When complete, output ONLY this JSON on the last line (no text after it):
{{"status":"DONE","critical_count":<N>,"high_count":<N>,"summary":"<one sentence covering top risk>"}}
Or on failure:
{{"status":"ERROR","reason":"<explanation>"}}
"""


# ── Main entry ─────────────────────────────────────────────────────────────────

@click.command()
@click.option("--repo-url", default=None, help="Git clone URL or HTTPS repo URL to audit")
@click.option("--branch", default=None, help="Branch to audit (default: BB_REPO_BRANCH for Bitbucket, GH_REPO_BRANCH for GitHub)")
@click.option("--commits", default=None, type=int, help="Number of commits to analyze (default: N_COMMITS config or 1000)")
@click.option("--focus", default=None, help="Audit focus: all|architecture|security|tests|duplicates|health")
@click.option("--skill", default="ygs-codebase-audit", show_default=True, help="Skill name override")
def main(repo_url: str | None, branch: str | None, commits: int | None, focus: str | None, skill: str) -> None:
    config = load_config()
    validate_claude_config(config)

    # Branch resolution: CLI flag > tracker-specific branch env var.
    # Use BB_REPO_BRANCH for Bitbucket/Jira repos, GH_REPO_BRANCH for GitHub repos.
    if not branch:
        tracker = (config.get("DEFAULT_TRACKER") or "").lower()
        has_bitbucket = bool(config.get("BITBUCKET_WORKSPACE") and config.get("BITBUCKET_REPO"))
        if tracker in ("jira", "bitbucket", "jira/bitbucket") or (has_bitbucket and tracker != "github"):
            branch = config.get("BB_REPO_BRANCH", "main")
        else:
            branch = config.get("GH_REPO_BRANCH", "main")
    n_commits = commits or int(config.get("N_COMMITS", "1000"))
    focus = focus or config.get("AUDIT_FOCUS", "all")
    max_code_size = int(config.get("MAX_AUDIT_SIZE", "1048576"))

    slack_commits, slack_max_size = _parse_slack_flags(config)
    if slack_commits and not commits:
        n_commits = slack_commits
        print(f"[audit] Slack override: n_commits={n_commits}", flush=True)
    if slack_max_size:
        max_code_size = slack_max_size
        print(f"[audit] Slack override: max_code_size={max_code_size // 1024}KB", flush=True)

    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    logs_dir = workspace / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    resolved_url = resolve_repo_url(config, repo_url)
    repo_label = compute_repo_label(config, resolved_url)

    print(f"[audit] repo={repo_label} branch={branch} commits={n_commits} focus={focus} max_code_size={max_code_size // 1024}KB", flush=True)
    print(f"::add-task-context AUDIT_REPO::{repo_label}", flush=True)
    print(f"::add-task-context AUDIT_BRANCH::{branch}", flush=True)
    print(f"::add-task-context AUDIT_COMMITS::{n_commits}", flush=True)
    print(f"::add-task-context AUDIT_FOCUS::{focus}", flush=True)
    print(f"::add-task-context AUDIT_MAX_CODE_SIZE::{max_code_size}", flush=True)

    _ensure_ygs_skills()

    # ── Clone repo if URL given, else use CODEBASE_DIR ──────────────────────
    repo_path: Path

    codebase_dir = os.environ.get("CODEBASE_DIR", "").strip()
    if resolved_url:
        # Clone into workspace/repo/ so Claude's working dir IS the repo root.
        # This lets Claude run grep/find/cat directly without absolute paths.
        # The workspace emptyDir volume is large enough; no tempdir needed.
        repo_path = workspace / "repo"
        tmp = None  # no tempdir to clean up
        clone_depth = max(n_commits + 100, 500)
        print(f"[audit] cloning {resolved_url} (branch={branch}, depth={clone_depth}) ...", flush=True)
        success, branch = clone_for_audit(resolved_url, branch, repo_path, depth=clone_depth)
        if not success:
            _write_audit_reports(workspace, {"repo": repo_label, "branch": branch, "commits_analyzed": 0,
                                              "focus": focus, "critical_count": 0, "high_count": 0, "findings": []})
            sys.exit(1)
        # Re-extract repo label from actual remote URL (more reliable than parsing clone URL)
        try:
            res = subprocess.run(["git", "remote", "get-url", "origin"],
                                 cwd=repo_path, capture_output=True, text=True, timeout=10)
            remote_url = res.stdout.strip()
            if remote_url:
                repo_label = compute_repo_label(config, remote_url)
        except Exception:
            pass
        # Update markers with actual resolved values (may differ from initial config)
        print(f"::add-task-context AUDIT_REPO::{repo_label}", flush=True)
        print(f"::add-task-context AUDIT_BRANCH::{branch}", flush=True)
    elif codebase_dir and (Path(codebase_dir) / ".git").exists():
        repo_path = Path(codebase_dir)
    else:
        print("[audit] no repo URL and no CODEBASE_DIR with .git — cannot audit", file=sys.stderr, flush=True)
        _write_audit_reports(workspace, {"repo": repo_label, "branch": branch, "commits_analyzed": 0,
                                          "focus": focus, "critical_count": 0, "high_count": 0, "findings": []})
        sys.exit(1)

    try:
        # Apply project-specific skill overrides first
        applied = apply_project_skills(repo_path)
        if applied:
            print(f"::add-task-context REPO_SKILLS_COUNT::{applied}", flush=True)

        # Build pre-computed git statistics (100k char context budget — Claude handles 200k+ tokens)
        print("[audit] computing git statistics ...", flush=True)
        audit_context = build_audit_context(
            repo_path, n_commits=n_commits, focus=focus,
            max_code_size=max_code_size, max_context_chars=100_000,
        )
        if audit_context:
            print(f"[audit] git context: {len(audit_context)} chars", flush=True)
        else:
            print("[audit] no git context — repo may have too few commits", flush=True)

        info = get_repo_info(repo_path, n_commits=n_commits)
        head_commit = info.get("head_commit", "")
        head_date = info.get("head_date", "")
        oldest_commit = info.get("oldest_commit", "")
        oldest_date = info.get("oldest_date", "")
        print(f"[audit] HEAD={head_commit} branch={info.get('branch')}", flush=True)
        if oldest_commit:
            print(f"[audit] commit range: {oldest_commit} ({oldest_date}) → {head_commit} ({head_date})", flush=True)
            print(f"::add-task-context AUDIT_COMMIT_FROM::{oldest_commit} ({oldest_date})", flush=True)
            print(f"::add-task-context AUDIT_COMMIT_TO::{head_commit} ({head_date})", flush=True)

        # ── Load audit skill (repo-local wins over ygs base) ────────────────
        _audit_skill_candidates = ["codebase-audit", skill, "ygs-codebase-audit"]
        skill_md: str | None = None
        actual_skill = skill
        for candidate in _audit_skill_candidates:
            skill_md = _load_skill_md(candidate)
            if skill_md:
                actual_skill = candidate
                break

        print(f"::add-task-context SKILL::{actual_skill}", flush=True)
        print(f"::add-task-context SKILL_LOADED::{'yes' if skill_md else 'no'}", flush=True)

        # ── Load dimension-specific skills ───────────────────────────────────
        # Repo-local .claude/skills/<dim>/SKILL.md overrides ygs defaults.
        # apply_project_skills() already symlinked repo skills into ~/.claude/skills/,
        # so _load_dimension_skill() finds them via the standard search path.
        # Repo-local .claude/skills/<dim>/SKILL.md is checked first via _skill_search_paths().
        # These inject supplemental dimension protocols as a "Dimension-Specific Protocols" block
        # in the prompt. The ygs-codebase-audit specialist files are loaded by Claude directly
        # via Read tool calls in the skill — no injection needed for those.
        arch_skill = _load_dimension_skill(
            "architecture",
            ["architecture-review", "ygs-review-architecture", "ygs-review-deep"],
            config,
        )
        sec_skill = _load_dimension_skill(
            "security",
            ["security-review", "ygs-security-review"],
            config,
        )
        sre_skill = _load_dimension_skill(
            "sre",
            ["sre-review", "ygs-sre-review", "observability"],
            config,
        )
        testing_skill = _load_dimension_skill(
            "testing",
            ["test-health", "testing-review", "ygs-testing"],
            config,
        )
        duplicates_skill = _load_dimension_skill(
            "duplicates",
            ["duplicate-abstractions", "ygs-duplicates"],
            config,
        )

        _DIM_CHAR_LIMIT = 4000

        def _dim_block(header: str, content: str) -> str:
            if len(content) > _DIM_CHAR_LIMIT:
                return f"### {header}\n\n{content[:_DIM_CHAR_LIMIT]}\n\n_(truncated for token budget)_"
            return f"### {header}\n\n{content}"

        dimension_blocks: list[str] = []
        if arch_skill:
            dimension_blocks.append(_dim_block("Architecture Review Protocol", arch_skill))
        if sec_skill:
            dimension_blocks.append(_dim_block("Security Review Protocol", sec_skill))
        if sre_skill:
            dimension_blocks.append(_dim_block("SRE / Operational Review Protocol", sre_skill))
        if testing_skill:
            dimension_blocks.append(_dim_block("Testing Review Protocol", testing_skill))
        if duplicates_skill:
            dimension_blocks.append(_dim_block("Duplicate Abstractions Protocol", duplicates_skill))

        if dimension_blocks:
            loaded_dims = [
                name for name, sk in [
                    ("architecture", arch_skill), ("security", sec_skill),
                    ("sre", sre_skill), ("testing", testing_skill), ("duplicates", duplicates_skill),
                ] if sk
            ]
            print(f"[audit] repo-specific dimension skills: {', '.join(loaded_dims)}", flush=True)

        dimension_skills_block = (
            "## Repo-Specific Dimension Protocols\n\n"
            "These repo-local protocols override the default specialist phases for their respective dimensions.\n\n"
            + "\n\n---\n\n".join(dimension_blocks)
            if dimension_blocks
            else ""
        )

        # ── Build prompt ─────────────────────────────────────────────────────
        commit_range_line = ""
        if oldest_commit and head_commit:
            commit_range_line = f"\n**Commit range**: {oldest_commit} ({oldest_date}) → {head_commit} ({head_date})"
        if skill_md:
            prompt = _AUDIT_PROMPT_TEMPLATE.format(
                repo_label=repo_label,
                branch=branch,
                n_commits=n_commits,
                focus=focus,
                audit_context=audit_context or "(no git statistics available)",
                skill_instructions=skill_md,
                dimension_skills_block=dimension_skills_block,
                commit_range_line=commit_range_line,
            )
        else:
            print(f"[audit] WARNING: no SKILL.md found for audit — using fallback prompt", flush=True)
            prompt = _FALLBACK_PROMPT

        (logs_dir / "audit.prompt.txt").write_text(prompt, encoding="utf-8")

        # Symlink repo_path/reports → workspace/reports so Claude can write ./reports/
        # (relative to repo root) and the files land in the standard artifact location.
        # Follows the same pattern as the implement self-review (working_dir=repo_dir).
        repo_reports_link = repo_path / "reports"
        if not repo_reports_link.exists():
            repo_reports_link.symlink_to(reports_dir.resolve())

        model = config.get("AI_MODEL")
        max_turns = int(config.get("MAX_TURNS_AUDIT", "80"))
        log_path = logs_dir / "audit.log"

        print(f"[audit] running with model={model} max_turns={max_turns}", flush=True)

        try:
            result = run_claude(
                prompt,
                working_dir=repo_path,
                model=model,
                max_turns=max_turns,
                log_file=log_path,
                allowed_tools="Bash,Read,Write,Edit,Glob,Grep,LS,Skill",
                system_prompt=SYSTEM_PROMPTS["review"],
                primary_skill=actual_skill,
            )
        except RuntimeError as e:
            print(f"ERROR: claude failed: {e}", file=sys.stderr, flush=True)
            _write_audit_reports(workspace, {
                "repo": repo_label, "branch": branch, "commits_analyzed": n_commits,
                "focus": focus, "critical_count": 0, "high_count": 0, "findings": [],
            })
            sys.exit(1)

        status_data: dict = result.status_json or {"status": result.status}
        (reports_dir / "result.json").write_text(json.dumps(status_data, indent=2), encoding="utf-8")

        # If Claude didn't write audit_report.md (or wrote a stub), recover from stdout or log.
        report_md_path = reports_dir / "audit_report.md"
        if not report_md_path.exists() or report_md_path.stat().st_size < 300:
            # Prefer stdout; fall back to the log file (which captures all Claude tool output).
            output_text = result.output or ""
            if len(output_text) < 200 and log_path.exists():
                try:
                    output_text = log_path.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
            output_lines = output_text.splitlines()
            analysis_lines = [ln for ln in output_lines
                              if not (ln.strip().startswith('{"status"') and "DONE" in ln)]
            analysis_text = "\n".join(analysis_lines).strip()
            if len(analysis_text) >= 50:
                report_md_path.write_text(
                    f"# Codebase Audit — {repo_label} @ {branch}\n\n{analysis_text}\n",
                    encoding="utf-8",
                )
                print(f"[audit] saved fallback audit_report.md ({len(analysis_text)} chars)", flush=True)

        findings_path = reports_dir / "audit_findings.json"
        _write_audit_reports(workspace, {
            "repo": repo_label, "branch": branch, "commits_analyzed": n_commits,
            "commit_from": oldest_commit, "commit_from_date": oldest_date,
            "commit_to": head_commit, "commit_to_date": head_date,
            "focus": focus, "critical_count": status_data.get("critical_count", 0),
            "high_count": status_data.get("high_count", 0), "findings": [],
        })

        # Patch Claude's JSON with commit-range and identity fields if absent/blank.
        # Claude writes the file; _write_audit_reports only fills it when missing.
        # This ensures post_audit.py always has the full picture regardless of what
        # Claude included.
        if findings_path.exists():
            try:
                fdata = json.loads(findings_path.read_text(encoding="utf-8"))
                patched = False
                for key, val in [
                    ("repo", repo_label), ("branch", branch),
                    ("commits_analyzed", n_commits), ("focus", focus),
                    ("commit_from", oldest_commit), ("commit_from_date", oldest_date),
                    ("commit_to", head_commit), ("commit_to_date", head_date),
                ]:
                    if not fdata.get(key):
                        fdata[key] = val
                        patched = True
                if patched:
                    findings_path.write_text(json.dumps(fdata, indent=2), encoding="utf-8")
                    print("[audit] patched audit_findings.json with commit range / identity fields", flush=True)
            except Exception as e:
                print(f"[audit] could not patch findings JSON: {e}", flush=True)

        if findings_path.exists():
            _emit_finding_counts(findings_path, fallback_repo=repo_label, fallback_branch=branch)
        else:
            crit = status_data.get("critical_count", 0)
            high = status_data.get("high_count", 0)
            print(f"::add-task-context AUDIT_CRITICAL_COUNT::{crit}", flush=True)
            print(f"::add-task-context AUDIT_HIGH_COUNT::{high}", flush=True)

        # Generate HTML report from Markdown
        report_md_path = reports_dir / "audit_report.md"
        report_html_path = reports_dir / "audit_report.html"
        if report_md_path.exists() and not report_html_path.exists():
            try:
                md_text = report_md_path.read_text(encoding="utf-8")
                html_text = render_simple_html(f"Codebase Audit — {repo_label}", md_text)
                report_html_path.write_text(html_text, encoding="utf-8")
                print(f"[audit] wrote reports/audit_report.html ({len(html_text)} chars)", flush=True)
            except Exception as e:
                print(f"[audit] WARNING: could not render HTML: {e}", flush=True)

        print(f"::add-task-context SELECTED_MODEL::{model or ''}", flush=True)

        status_val = status_data.get("status", "")
        summary = status_data.get("summary", "")
        print(f"[audit] status={status_val} critical={status_data.get('critical_count', 0)} "
              f"high={status_data.get('high_count', 0)}", flush=True)
        if summary:
            print(f"[audit] summary: {summary}", flush=True)

        if status_val in ("DONE", "MAX_TURNS_REACHED"):
            sys.exit(0)

        print(f"ERROR: unexpected audit status '{status_val}'", file=sys.stderr, flush=True)
        sys.exit(1)

    except Exception as e:
        print(f"ERROR: audit failed: {e}", file=sys.stderr, flush=True)
        _write_audit_reports(workspace, {
            "repo": repo_label, "branch": branch, "commits_analyzed": n_commits,
            "focus": focus, "critical_count": 0, "high_count": 0, "findings": [],
        })
        sys.exit(1)


if __name__ == "__main__":
    main()
