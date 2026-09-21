"""PR Audit — analyze last N merged PRs for spec/design/skills gaps.

Usage:
    python -m scripts.analyze.run_pr_audit [--repo-url URL] [--n-prs 50]
        [--focus all|spec|design|skills|practices]

Required env: ANTHROPIC_API_KEY or CLAUDE_CODE_USE_BEDROCK=1

Repo URL resolution (in priority order):
  1. --repo-url flag
  2. CODEBASE_REPO_URL env
  3. DEFAULT_TRACKER=jira/bitbucket -> BITBUCKET_WORKSPACE + BITBUCKET_REPO
  4. DEFAULT_TRACKER=github -> GH_ORG + GH_REPO

Writes:
  /workspace/reports/pr_audit_findings.json
  /workspace/reports/pr_audit_report.md
  /workspace/reports/pr_audit_report.html
  /workspace/reports/skill_improvements.json
  /workspace/logs/pr_audit.log

Exit codes: 0=done, 1=error
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import click

from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS, ensure_ygs_skills
from scripts.common.config import get_workspace_dir, load_config, validate_claude_config
from scripts.common.repo_utils import resolve_repo_url, repo_label as compute_repo_label, clone_for_audit
from scripts.common.report_renderer import render_simple_html
from scripts.common.skills import apply_project_skills, inline_shared_refs
from scripts.analyze.pr_fetcher import (
    fetch_prs, fetch_prs_by_numbers, parse_pr_url,
    classify_comments, link_pr_to_issue,
    fetch_issue_details, build_pr_context, write_issues_raw,
    compute_pr_state_summary, size_bucket,
)


# -- Skill discovery (same pattern as run_codebase_audit.py) -------------------

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
            print(f"[pr-audit] skill path: {candidate}", flush=True)
            content = candidate.read_text(encoding="utf-8")
            return inline_shared_refs(content)
    return None


# -- Tracker resolution --------------------------------------------------------

def _resolve_effective_tracker(config: dict, slack_flags: dict) -> str:
    """Return the effective tracker using strict priority order.

    Priority (highest → lowest):
      1. Explicit ``--tracker <value>`` flag in the Slack message / prompt.
      2. Tracker derived from the first PR URL in the message (github.com → github,
         atlassian.net/bitbucket.org → jira).
      3. ``DEFAULT_TRACKER`` from job config / environment.

    This ensures any prompt can override the job-level default, so e.g.
    ``@bot pr-audit --tracker jira`` works even when the submitted
    job definition has ``DEFAULT_TRACKER=github``.
    """
    # 1. Explicit --tracker flag wins
    if slack_flags.get("tracker"):
        return slack_flags["tracker"].lower()
    # 2. Derived from first PR URL in the message
    if slack_flags.get("pr_urls"):
        try:
            tracker_type, _ = parse_pr_url(slack_flags["pr_urls"][0])
            return tracker_type
        except ValueError:
            pass
    # 3. Fall back to job-level default
    return (config.get("DEFAULT_TRACKER") or "").lower()


def _resolve_branch(config: dict, effective_tracker: str) -> str:
    """Return the default branch for the effective tracker."""
    has_bitbucket = bool(config.get("BITBUCKET_WORKSPACE") and config.get("BITBUCKET_REPO"))
    if effective_tracker in ("jira", "bitbucket", "jira/bitbucket") or (
        has_bitbucket and effective_tracker != "github"
    ):
        return config.get("BB_REPO_BRANCH", "main")
    return config.get("GH_REPO_BRANCH", "main")


# -- Slack flag parsing --------------------------------------------------------

def _parse_slack_flags(config: dict) -> dict:
    """Parse inline Slack flags from SLACK_MESSAGE env var.

    Supports:
      audit last 30 prs
      pr audit focus skills
      --n-prs 100 --focus design
      https://github.com/org/repo/pull/123
      https://bitbucket.org/ws/repo/pull-requests/456
      --model claude-opus-5
      model: claude-opus-5
      --team alice,bob               (filter by GitHub logins or Jira display names)
      team:alice,bob
      --team MyTeam                  (single word → Jira-issue-first filter by Eng Scrum Team field)
      --milestone v2.5               (filter by GitHub milestone)
      --filter label=security        (GitHub label filter)
      --filter "Eng Scrum Team"=MyTeam  (Jira field=value filter)

    Returns dict with keys: n_prs, focus, pr_urls, model, team_members, jira_boards, gh_milestone,
    tracker, full_report, jira_team, pr_filter.
    """
    msg = config.get("SLACK_MESSAGE", os.environ.get("SLACK_MESSAGE", ""))
    if not msg:
        return {"n_prs": None, "focus": None, "pr_urls": [], "model": None,
                "team_members": None, "jira_boards": None, "gh_milestone": None,
                "tracker": None, "full_report": False,
                "jira_team": None, "pr_filter": None}

    msg_lower = msg.lower()

    n_prs: int | None = None
    focus: str | None = None
    model: str | None = None
    pr_urls: list[str] = []
    team_members: str | None = None
    jira_boards: str | None = None
    gh_milestone: str | None = None

    m = re.search(r"(?:last\s+)?(\d+)\s+prs?|--n-prs\s+(\d+)", msg_lower)
    if m:
        val = m.group(1) or m.group(2)
        if val:
            n_prs = int(val)

    m = re.search(r"focus\s+(all|spec|design|skills|practices)|--focus\s+(\S+)", msg_lower)
    if m:
        focus = m.group(1) or m.group(2)

    # Model override: --model <name> or model: <name> (word boundary prevents matching "normalmodel:")
    m = re.search(r"--model\s+(\S+)|\bmodel[:=]\s*(\S+)", msg, re.IGNORECASE)
    if m:
        model = m.group(1) or m.group(2)

    # Team filter: --team alice,bob (commas → member list) | --team MyTeam (no comma → Jira team)
    # team:alice,bob also accepted for backward compat.
    # Unified parse: comma → team_members; single word → jira_team.
    jira_team: str | None = None
    m = re.search(r"(?:--team|team:)\s*([\w,@\-\.]+)", msg, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        if "," in val:
            team_members = val   # comma-sep display names → PR author/reviewer filter
        else:
            jira_team = val      # single word → Jira-issue-first team filter

    # --board is still parsed for backward compat with JIRA_BOARDS env configs.
    # New behavior: team is auto-detected from Jira account; board ID only used as
    # sprint-fetch fallback when team auto-detection fails.
    m = re.search(r"--board(?:\s+(\d+))?(?=\s|$)", msg, re.IGNORECASE)
    if m:
        jira_boards = m.group(1) if m.group(1) else "__default__"
    else:
        m = re.search(r"\bboard:\s*(\d+)", msg, re.IGNORECASE)
        if not m:
            m = re.search(r"boards/(\d+)", msg)
        if m:
            jira_boards = m.group(1)

    # GitHub milestone: --milestone <name>
    m = re.search(r"--milestone\s+(\S+)", msg, re.IGNORECASE)
    if m:
        gh_milestone = m.group(1).strip()

    # Tracker override: --tracker github|jira|bitbucket
    # This lets any prompt explicitly set the tracker regardless of DEFAULT_TRACKER.
    tracker: str | None = None
    m = re.search(r"--tracker\s+(github|jira(?:/bitbucket)?|bitbucket)", msg, re.IGNORECASE)
    if m:
        tracker = m.group(1).lower()

    # Full report flag: --full posts the complete report to Slack instead of the digest.
    full_report = bool(re.search(r"--full\b", msg, re.IGNORECASE))

    # Explicit field=value filter: --filter <field>=<value> (Jira field or label=X for GH).
    pr_filter: str | None = None
    m = re.search(r'--filter\s+"?([^"=\n]+?)"?\s*=\s*("?[^"\s\n]+"?)', msg, re.IGNORECASE)
    if m:
        pr_filter = f"{m.group(1).strip()}={m.group(2).strip().strip(chr(34))}"

    # PR URLs: extract any GitHub or Bitbucket PR URLs from the message
    for token in re.findall(r"https?://\S+", msg):
        token = token.rstrip(".,;)")
        try:
            parse_pr_url(token)  # validate format; also implies tracker
            pr_urls.append(token)
        except ValueError:
            pass

    return {
        "n_prs": n_prs, "focus": focus, "pr_urls": pr_urls, "model": model,
        "team_members": team_members, "jira_boards": jira_boards,
        "gh_milestone": gh_milestone, "tracker": tracker, "full_report": full_report,
        "jira_team": jira_team, "pr_filter": pr_filter,
    }


# -- Markers -------------------------------------------------------------------

def _emit_finding_counts(findings_path: Path, fallback_repo: str = "", fallback_branch: str = "") -> None:
    """Parse pr_audit_findings.json and emit ::add-task-context markers."""
    try:
        data = json.loads(findings_path.read_text(encoding="utf-8"))
        spec_gaps = data.get("spec_gap_count", 0)
        design_gaps = data.get("design_gap_count", 0)
        skill_gaps = data.get("skill_gap_count", 0)
        practice_gaps = data.get("practice_gap_count", 0)
        repo = data.get("repo") or fallback_repo
        branch = data.get("branch") or fallback_branch
        prs_analyzed = data.get("prs_analyzed", 0)
        focus = data.get("focus", "all")
        if repo:
            print(f"::add-task-context PR_AUDIT_REPO::{repo}", flush=True)
        if branch:
            print(f"::add-task-context PR_AUDIT_BRANCH::{branch}", flush=True)
        if prs_analyzed:
            print(f"::add-task-context PR_AUDIT_PRS::{prs_analyzed}", flush=True)
        print(f"::add-task-context PR_AUDIT_FOCUS::{focus}", flush=True)
        print(f"::add-task-context PR_AUDIT_SPEC_GAPS::{spec_gaps}", flush=True)
        print(f"::add-task-context PR_AUDIT_DESIGN_GAPS::{design_gaps}", flush=True)
        print(f"::add-task-context PR_AUDIT_SKILL_GAPS::{skill_gaps}", flush=True)
        print(f"::add-task-context PR_AUDIT_PRACTICE_GAPS::{practice_gaps}", flush=True)
        pr_ids = data.get("pr_ids", "")
        if pr_ids:
            print(f"::add-task-context PR_AUDIT_PR_IDS::{pr_ids}", flush=True)
        date_from = data.get("date_from", "")
        date_to = data.get("date_to", "")
        if date_from:
            print(f"::add-task-context PR_AUDIT_DATE_FROM::{date_from}", flush=True)
            print(f"::add-task-context PR_AUDIT_DATE_TO::{date_to}", flush=True)
        jiras_reviewed = data.get("jiras_reviewed", 0)
        if jiras_reviewed:
            print(f"::add-task-context PR_AUDIT_JIRAS_REVIEWED::{jiras_reviewed}", flush=True)
        metrics = data.get("metrics", {})
        if isinstance(metrics, dict):
            verbosity_rate = metrics.get("verbosity_accumulation_rate", 0)
            complexity_prs = metrics.get("complexity_creep_pr_count", 0)
            if verbosity_rate:
                print(f"::add-task-context PR_AUDIT_VERBOSITY_RATE::{verbosity_rate:.2f}", flush=True)
            if complexity_prs:
                print(f"::add-task-context PR_AUDIT_COMPLEXITY_CREEP::{complexity_prs}", flush=True)
    except Exception as e:
        print(f"[pr-audit] could not parse findings for markers: {e}", flush=True)


def _write_pr_audit_reports(workspace: Path, stub: dict) -> None:
    """Ensure reports/pr_audit_findings.json and reports/pr_audit_report.md exist."""
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    findings_path = reports_dir / "pr_audit_findings.json"
    if not findings_path.exists():
        findings_path.write_text(json.dumps(stub, indent=2), encoding="utf-8")
        print("[pr-audit] wrote stub pr_audit_findings.json", flush=True)

    report_path = reports_dir / "pr_audit_report.md"
    if not report_path.exists():
        report_path.write_text(
            f"# PR Audit -- {stub.get('repo', 'unknown')}\n\n"
            "PR audit did not complete -- see pr_audit.log for details.\n",
            encoding="utf-8",
        )
        print("[pr-audit] wrote stub pr_audit_report.md", flush=True)


# -- Prompt template -----------------------------------------------------------

_FALLBACK_PROMPT = """\
You are a principal engineer performing a PR audit on the last {n_prs} merged pull requests.

Analyze the PRs for:
1. Spec gaps -- PRs without linked issues or acceptance criteria
2. Design gaps -- large PRs, missing design docs, architectural decisions not documented
3. Skill gaps -- repeated review feedback patterns, same mistakes across PRs
4. Practice gaps -- missing tests, no code review, inconsistent PR descriptions

Write findings to:
- reports/pr_audit_findings.json (see format in instructions)
- reports/pr_audit_report.md (markdown report with executive summary)
- reports/skill_improvements.json (proposed skill/doc improvements)

Output ONLY this JSON on the last line:
{{"status":"DONE","spec_gap_count":<N>,"design_gap_count":<N>,"skill_gap_count":<N>,"practice_gap_count":<N>,"summary":"<one sentence>"}}
Or on failure:
{{"status":"ERROR","reason":"<explanation>"}}
"""

_PR_AUDIT_PROMPT_TEMPLATE = """\
## PR Audit Task

**Repository**: {repo_label}
**Branch**: {branch}
**PRs analyzed**: {n_prs}
**Date range**: {date_from} to {date_to}
**Jira issues reviewed**: {jiras_reviewed}
**Focus**: {focus}
**Working directory**: You are in the cloned repository root -- run `grep`, `find`, `cat`, etc. directly on the source files.
**Reports directory**: Write output files to `./reports/` (relative path, same as other workflows).

## Audit Instructions

{skill_instructions}

## PR State Summary

{pr_state_summary}

## MANDATORY: PR State Gate — No exceptions

Before writing ANY finding, check the PR's `state` field:

- **state=merged** — This PR LANDED in the codebase. Gaps here are real risks. Generate findings normally.
- **state=open** — This PR is currently IN REVIEW. Frame as a current risk, not a historical failure.
- **state=declined** — This PR was REJECTED. The review process WORKED. **DO NOT generate "merged without review" or "scope explosion that landed" findings for declined PRs.** A declined bot-authored PR with zero reviewers is evidence the process caught an issue, not evidence of a gap. Only flag a declined PR if it had zero review activity AND zero comments before decline (pure rubber-stamp decline of a bad PR without any engagement).

Violating this rule creates false positives that destroy audit credibility.

## Merged PR Data

The following PR data has been pre-fetched from the issue tracker.
Use the PR data to identify patterns, then verify findings by examining the actual code.

{pr_context}

## CRITICAL: Analyze ALL {n_prs} PRs

You MUST analyze EVERY PR in the data above -- not just a handful of interesting ones.
A common failure mode is deeply analyzing 8-10 PRs and ignoring the rest. This is NOT acceptable.

For EVERY PR:
- Check spec coverage (linked issue, acceptance criteria present or absent)
- Check design doc presence (for PRs with complex architectural changes, NOT based on LOC alone)
- Check bot vs human comment patterns (skill gaps)
- Check test coverage, review quality, PR size, practices

Then synthesize cross-PR patterns. The value of this audit is the patterns across ALL {n_prs} PRs,
not a deep-dive into a few.

## CRITICAL: Pattern frequency determines finding priority

A finding seen in 1 PR is an observation. A finding seen in 3+ PRs is a systemic gap worth acting on.
- **PRIORITIZE** patterns appearing in 3+ PRs — these are the only ones worth skill changes
- **DEPRIORITIZE** one-off observations — mention them only in a "Single-PR observations" appendix
- **QUANTIFY** every finding in the executive summary: "X/{n_prs} PRs missing Y" not "some PRs lacked Y"
- For each finding in the report, state its frequency explicitly: "8/{n_prs} PRs", "seen in PRs #12, #34, #67"

## CRITICAL: Classify each gap by type and impact

For every finding, classify it along two axes:

**Gap type** (what kind of gap):
- `team_skill` — engineers repeatedly make the same mistake (needs training/skill docs)
- `process` — the workflow is missing a step (needs process change)
- `tooling` — automation could catch this but doesn't exist yet (needs tooling)

**Impact type** (what it causes):
- `blocks_delivery` — causes build failures, blocks merge, directly delays shipping
- `slows_delivery` — increases review cycles, causes rework, slows velocity
- `increases_risk` — ships but with latent bugs, security issues, or operational fragility

State both classifications in each finding. Example: "gap_type: team_skill, impact: increases_risk"

## CRITICAL: Acceptance criteria detection — no false positives

The `has_acceptance_criteria` field in the pre-computed data has three possible values:
- `true` — semantic detection found AC (checkboxes, BDD, numbered requirements, etc.)
- `false` — issue was accessible but no testable requirements found in any form
- `unknown (Jira access denied -- restricted project)` — the issue EXISTS but the audit bot could not read it

**When `has_acceptance_criteria: false`**: verify by reading the `Issue description excerpt` — the issue
may describe requirements using different language. Only flag as "AC missing" if you can confirm the
description lacks any testable requirements.

**When `has_acceptance_criteria: unknown (access denied)`**: you have NO DATA about this issue. Do NOT
flag it as "AC missing". Do NOT include it in the AC coverage denominator. Treat it identically to
"no linked issue" for metrics purposes.

**Never report "X/N PRs missing AC" when many of those X are access-denied issues** — that is a false
positive. Only count PRs where you can actually read the issue and confirm it lacks AC.

## CRITICAL: Include PR IDs everywhere

Every finding MUST reference specific PR numbers (e.g., "PR #46468", "PRs #47554, #47533").
The report header MUST include the count of PRs analyzed.
The findings JSON MUST include `pr_number` or `prs` array for every finding.

## REQUIRED: Additional analysis dimensions

Beyond the 4 specialist dimensions, also check for:
- **Conflicting changes**: PRs that modify the same files/modules with divergent intent
- **Duplicate abstractions**: PRs introducing new abstractions that overlap with existing ones
- **Brittle tests**: Tests using sleep/timing, excessive mocking, environment-dependent assertions
- **Industry practice violations**: Missing rollback plans, rubber-stamp reviews on risky changes,
  no documentation updates for new features
- **Knowledge base / second brain**: Check if the repo has `docs/adr/`, `adr/`, `design/decisions/`, or
  similar. If not, and design debates are happening in PR comments, recommend establishing one. The goal
  is a permanent, searchable decision log where each entry answers: "What did we consider? What did we
  decide? Why?"
- **Blast-radius review adequacy**: When assessing review quality, prioritize blast-radius over LOC.
  A 2-line change to auth/ACL/config/flags can have higher risk than a 300-line refactor. Always note
  the blast-radius reason when flagging zero-review changes.
- **Rubber-stamp detection**: Each PR includes pre-computed `Substantive human comments` count and
  `Rubber-stamp approvers` list. Use these directly instead of manually scanning comment text.
  Rubber-stamp = approver who left zero substantive comments (silent approval, LGTM, +1, looks good,
  ship it, emoji-only, or other single-phrase approvals with no technical content).
  Rubber-stamp on HIGH blast-radius (auth/billing/query-engine/infra/config) = HIGH finding.
  Rubber-stamp on LOW blast-radius = informational only. Always distinguish from "no human review" —
  rubber-stamp means PRESENT but shallow; no review means ABSENT entirely.
- **Bot-authored PRs**: Each PR includes a pre-computed `Author type: AI/bot-authored PR` flag.
  Bot-authored PRs require ≥1 substantive human comment from a reviewer who can verify correctness.
  Silent approval or LGTM on a bot-authored PR is insufficient — bots optimize for stated spec and
  miss emergent interactions. Flag bot-authored PRs with 0 substantive comments as MEDIUM risk;
  flag bot-authored PRs touching high-blast-radius files with 0 substantive comments as HIGH.
- **Recommendations must NOT cite arbitrary LOC thresholds or repo-specific directory paths.**
  Write recommendations in terms of risk categories (e.g., "PRs touching auth, feature flags,
  or infra should require ≥1 human reviewer") not specific numbers and paths ("PRs with >400 LOC
  in src/"). LOC is a loose signal, not a policy gate.
- **Review activities include approvals**: The PR context includes an "Approved by" field listing
  Bitbucket approvers, and a "Review decision" field. A PR that shows approvers or an APPROVED
  decision IS human-reviewed — even if the approver left no inline comments. Do NOT count it as
  "no human review". Only flag PRs where `Approved by` is absent AND human comments are absent.
  A silent approval on a high-blast-radius change is still a rubber-stamp concern — but it is a
  different finding ("insufficient scrutiny") than "no human review".
- **AC detection is semantic**: When `has_acceptance_criteria: false`, read the "Issue description
  excerpt" carefully. Prose that clearly describes the desired fix or behavior (e.g., "Fix —
  wire up event-driven refresh instead of backoff") satisfies the intent of AC. Only flag as
  missing if the description provides no testable expected behavior at all.

## REQUIRED: Distinguish CI bots vs code-review bots

The PR context labels each PR's comments into three groups:
- `ci_comments` (or "CI bot comments"): build runners, test status reporters, linters. Their reports are build failures — NOT code-review findings.
- `review_bot_comments` (or "Code-review bot comments"): Claude PR Review Agent, CodeRabbit, SonarCloud, etc. Their comments are code-quality observations.
- `human_comments`: human reviewers.

For the Metrics Dashboard, compute SEPARATE rates:
- **CI catch rate** = CI bot catches / (CI bot + human catches) — measures pipeline health
- **Code-review skill catch rate** = review-bot catches / (review-bot + human catches) — measures review skill quality

NEVER report a single combined "skill catch rate" that mixes CI bots with code-review bots. The two metrics tell different stories.

## REQUIRED: Skills assessment summary

Include a dedicated section in the report analyzing what skills/capabilities were demonstrated
and what was lacking across all PRs:
- Coding skills (correctness, error handling, performance)
- Review skills (thoroughness, domain knowledge, constructive feedback)
- Testing skills (coverage, edge cases, integration testing)
- SRE/operational skills (monitoring, rollback plans, feature flags)
- Security awareness (auth, input validation, secrets management)
- Architecture skills (modularity, separation of concerns, API design)

Rate each as Strong/Developing/Gap based on evidence from the PR data.

## CRITICAL: Security skill recommendations

When reporting on security review coverage:
- Check whether the repo has a `.claude/skills/security-review/` skill (or similar) before
  reporting on invocation rate — if no such skill exists, the rate is 0% by definition and
  reporting it as a gap is misleading.
- **Correct recommendation when security coverage is low**: advise the repo to **add or improve
  a `.claude/skills/security-review/SKILL.md`** in the repo itself. You may mention
  `/ygs-security-review` as a reference example they can copy from, but do not tell teams to
  invoke a skill they don't have.
- Never recommend invoking `/ygs-security-review` as an action item unless you confirmed it
  exists in the repo's `.claude/skills/` directory.

## REQUIRED: Positive Patterns

Include a dedicated **Positive Patterns** section in the report that highlights exemplary behavior:
- Human reviewers who demonstrated deep domain expertise (e.g., "Abbas Mashayekh caught an auth
  bypass in PR #xxx before merge")
- Reviewers who gave constructive, detailed feedback that prevented rework
- PRs with exceptional test coverage or well-structured rollback plans
- Teams or individuals who consistently catch the right issues early

This section reinforces good behavior and gives the team recognition for what they are doing well.
It should appear after the findings sections and before the Metrics Dashboard.

## REQUIRED OUTPUTS -- Must be written before emitting the final JSON line

Write these files using relative paths from the repo root (the `reports/` symlink resolves to the workspace reports directory):

1. `reports/pr_audit_report.md` -- Comprehensive markdown PR audit report:
   - **Executive summary** (3-4 sentences):
     - Lead with the most frequent gap: "X/{n_prs} PRs [specific problem]"
     - State the top gap type (team_skill / process / tooling) and impact type
     - Name the highest-severity finding with its PR count
   - **Critical/High/Medium/Low findings**: every finding title includes PR count + IDs
     e.g. "## HIGH: Missing error handling in async paths (6/{n_prs} PRs: #123, #145, #167, #189, #201, #223)"
     Each finding must state: gap_type, impact_type, evidence (specific PRs + what was observed),
     and a concrete recommendation targeting that specific pattern
   - **Skills Assessment** rating coding/review/testing/SRE/security/architecture as Strong/Developing/Gap
     with evidence: "Gap — PRs #34, #67, #89 all introduced SQL queries without parameterization"
   - **Recommended Skill Updates**: only for gaps seen in 3+ PRs. For each, state:
     - File path in `.claude/skills/`
     - Which PRs motivated it (minimum 3)
     - What the skill file currently says (if it exists) vs what needs to change
   - **Metrics Dashboard**: spec coverage %, CI catch rate, review-bot catch rate, human review burden %
   - **Single-PR observations** appendix: one-off issues that don't warrant skill changes
   - **Checked — No Issues Found** section (proves thoroughness)
   - Minimum 2000 chars. If you write less, you did not analyze enough PRs.

2. `reports/pr_audit_findings.json` -- Structured JSON:
   {{"repo":"{repo_label}","branch":"{branch}","prs_analyzed":{n_prs},"focus":"{focus}",
    "pr_ids":"{pr_ids}","date_from":"{date_from}","date_to":"{date_to}","jiras_reviewed":{jiras_reviewed},
    "spec_gap_count":N,"design_gap_count":N,"skill_gap_count":N,"practice_gap_count":N,
    "findings":[{{"severity":"CRITICAL|HIGH|MEDIUM|LOW","category":"spec|design|skill|practice",
      "gap_type":"team_skill|process|tooling","impact_type":"blocks_delivery|slows_delivery|increases_risk",
      "frequency":N,"pr_number":N,"prs":[N,M],"title":"...","evidence":"specific finding","recommendation":"specific action"}}],
    "patterns":[{{"pattern":"description","frequency":N,"prs":[1,2,3],"gap_type":"team_skill|process|tooling","recommendation":"..."}}],
    "skills_assessment":{{"coding":"Strong|Developing|Gap","review":"...","testing":"...","sre":"...","security":"...","architecture":"..."}},
    "metrics":{{"spec_coverage_pct":0.0,"ci_catch_rate":0.0,"code_review_skill_catch_rate":0.0,
      "bot_finding_follow_through_rate":0.0,"human_review_burden":0.0,
      "pr_state_merged":0,"pr_state_open":0,"pr_state_declined":0,
      "avg_pr_size_loc":0,"median_pr_size_loc":0,
      "size_bucket_xs":0,"size_bucket_s":0,"size_bucket_m":0,"size_bucket_l":0,"size_bucket_xl":0,
      "large_pr_review_depth":0.0,"xl_pr_review_coverage_pct":0.0,
      "large_pr_human_comments_avg":0.0,
      "security_review_invocation_rate":0.0,
      "rubber_stamp_rate":0.0,"revert_followup_rate":0.0,
      "verbosity_accumulation_rate":0.0,"complexity_creep_pr_count":0}}}}

   Severity definitions (use these — do not invent your own):
   - CRITICAL: security vulnerability, data loss, or production outage risk
   - HIGH: incorrect behavior shipped to users, or gap seen in 5+ PRs causing rework
   - MEDIUM: gap seen in 3-4 PRs that slows delivery or increases risk
   - LOW: gap seen in 1-2 PRs, or a style/process issue with minimal impact

3. `reports/skill_improvements.json` -- Proposed improvements grounded ONLY in observed patterns:
   {{"repo_skill_changes":[{{"action":"update|create","file_path":"relative/path","description":"what to change","changes":"content to write"}}],
    "new_docs":[{{"path":"relative/path","description":"what this doc covers","content":"full content"}}],
    "ygs_recommendations":[{{"skill":"skill-name","recommendation":"what to improve"}}]}}

   CRITICAL RULES for skill_improvements:
   - Every change MUST cite 3+ specific PRs as evidence (e.g. "PRs #123, #456, #789 all missed X")
   - Before proposing a change to a `.claude/skills/` file, read that file — do NOT duplicate what is already there
   - NEVER propose generic industry rules (LOC thresholds, reviewer count formulas, mandatory review checklists)
     without 3+ PRs showing the team is missing that specific practice
   - NEVER invent process overhead without evidence that it would have caught real bugs in these PRs
   - The goal is to address RECURRING GAPS specific to this codebase and team
   - If you cannot cite 3+ PRs, do not propose a skill change — add it to the single-PR observations appendix instead

DO NOT emit any ::add-task-context markers yourself -- the orchestrator script reads
your JSON output and emits them automatically. Focus only on writing the three report files.

---

When complete, output ONLY this JSON on the last line (no text after it):
{{"status":"DONE","spec_gap_count":<N>,"design_gap_count":<N>,"skill_gap_count":<N>,"practice_gap_count":<N>,"summary":"<one sentence covering top gap area>"}}
Or on failure:
{{"status":"ERROR","reason":"<explanation>"}}
"""


# -- Main entry ----------------------------------------------------------------

@click.command()
@click.option("--repo-url", default=None, help="Git clone URL or HTTPS repo URL to audit")
@click.option("--branch", default=None, help="Branch to audit (default: BB_REPO_BRANCH or GH_REPO_BRANCH)")
@click.option("--n-prs", default=None, type=int, help="Number of merged PRs to analyze (default: N_PRS config or 50)")
@click.option("--focus", default=None, help="Audit focus: all|spec|design|skills|practices")
@click.option("--skill", default="ygs-pr-audit", show_default=True, help="Skill name override")
@click.option("--pr-urls", multiple=True, default=(), help="Specific PR URLs to audit (overrides --n-prs)")
def main(repo_url: str | None, branch: str | None, n_prs: int | None, focus: str | None, skill: str, pr_urls: tuple) -> None:
    config = load_config()
    validate_claude_config(config)

    n_prs = n_prs or int(config.get("N_PRS", "50"))
    focus = focus or config.get("PR_AUDIT_FOCUS", "all")
    max_pr_size = int(config.get("MAX_PR_AUDIT_SIZE", "10000000"))

    # Parse Slack flags first so tracker and branch resolution use prompt values.
    slack_flags = _parse_slack_flags(config)

    # Resolve effective tracker: --tracker flag > PR URL detection > DEFAULT_TRACKER config.
    effective_tracker = _resolve_effective_tracker(config, slack_flags)
    if effective_tracker:
        config["DEFAULT_TRACKER"] = effective_tracker
        if slack_flags.get("tracker"):
            print(f"[pr-audit] Slack override: tracker={effective_tracker}", flush=True)

    # Branch resolution uses the effective tracker so --tracker overrides it too.
    if not branch:
        branch = _resolve_branch(config, effective_tracker)

    if slack_flags["n_prs"] is not None:
        n_prs = slack_flags["n_prs"]
        print(f"[pr-audit] Slack override: n_prs={n_prs}", flush=True)
    if slack_flags["focus"] is not None:
        focus = slack_flags["focus"]
        print(f"[pr-audit] Slack override: focus={focus}", flush=True)
    if slack_flags["model"]:
        config["AI_MODEL"] = slack_flags["model"]
        print(f"[pr-audit] Slack override: model={slack_flags['model']}", flush=True)
    if slack_flags["team_members"]:
        os.environ["PR_AUDIT_TEAM_MEMBERS"] = slack_flags["team_members"]
        config["PR_AUDIT_TEAM_MEMBERS"] = slack_flags["team_members"]
        print(f"[pr-audit] Slack override: team={slack_flags['team_members']}", flush=True)
    if slack_flags["jira_boards"]:
        board_val = slack_flags["jira_boards"]
        if board_val == "__default__":
            # --board with no ID → fall back to JIRA_BOARDS org config (same key standup uses)
            board_val = config.get("JIRA_BOARDS", "")
            if not board_val:
                print("[pr-audit] --board used but JIRA_BOARDS org config not set", flush=True)
        if board_val:
            has_jira_creds = all([
                config.get("JIRA_BASE_URL"), config.get("JIRA_EMAIL"), config.get("JIRA_API_TOKEN"),
            ])
            if effective_tracker == "github" and not has_jira_creds:
                print(
                    "[pr-audit] WARNING: --board requires Jira credentials (JIRA_BASE_URL/EMAIL/TOKEN); "
                    "use --milestone to scope GitHub PRs by milestone instead",
                    flush=True,
                )
            else:
                os.environ["JIRA_BOARDS"] = board_val
                config["JIRA_BOARDS"] = board_val
                print(f"[pr-audit] Slack override: jira_boards={board_val}", flush=True)
    if slack_flags["gh_milestone"]:
        os.environ["PR_AUDIT_GH_MILESTONE"] = slack_flags["gh_milestone"]
        config["PR_AUDIT_GH_MILESTONE"] = slack_flags["gh_milestone"]
        print(f"[pr-audit] Slack override: milestone={slack_flags['gh_milestone']}", flush=True)
    if slack_flags["full_report"]:
        os.environ["AUDIT_FULL_REPORT"] = "1"
        config["AUDIT_FULL_REPORT"] = "1"
        print("[pr-audit] Slack override: full_report=1 (posting complete report to Slack)", flush=True)
    # Tracker-independent team filter: --team <name> resolves Jira issues by team field.
    # Fallback chain: Slack --team flag → JIRA_SPACE org config (JiraSpace / TeamId alias).
    effective_team = (slack_flags.get("jira_team") or config.get("JIRA_SPACE", "")).strip()
    if effective_team:
        os.environ["JIRA_SPACE"] = effective_team
        config["JIRA_SPACE"] = effective_team
        print(f"[pr-audit] team filter: {effective_team}", flush=True)
    if slack_flags.get("pr_filter"):
        os.environ["PR_AUDIT_FILTER"] = slack_flags["pr_filter"]
        config["PR_AUDIT_FILTER"] = slack_flags["pr_filter"]
        print(f"[pr-audit] Slack override: filter={slack_flags['pr_filter']}", flush=True)
    # Combine PR URLs: CLI flag + Slack message + PR_URLS env var (space/comma separated)
    pr_urls_env = [u.strip() for u in re.split(r"[\s,]+", os.environ.get("PR_URLS", "")) if u.strip()]
    all_pr_urls = list(pr_urls) + slack_flags["pr_urls"] + pr_urls_env

    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    logs_dir = workspace / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    reports_dir = workspace / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    resolved_url = resolve_repo_url(config, repo_url)
    label = compute_repo_label(config, resolved_url)

    print(f"[pr-audit] repo={label} branch={branch} n_prs={n_prs} focus={focus}", flush=True)
    print(f"::add-task-context PR_AUDIT_REPO::{label}", flush=True)
    print(f"::add-task-context PR_AUDIT_BRANCH::{branch}", flush=True)
    print(f"::add-task-context PR_AUDIT_N_PRS::{n_prs}", flush=True)
    print(f"::add-task-context PR_AUDIT_FOCUS::{focus}", flush=True)
    print(f"::add-task-context AUDIT_FULL_REPORT::{config.get('AUDIT_FULL_REPORT', '')}", flush=True)

    ensure_ygs_skills()

    # -- Clone repo if URL given, else use CODEBASE_DIR -----------------------
    repo_path: Path
    codebase_dir = os.environ.get("CODEBASE_DIR", "").strip()
    if resolved_url:
        repo_path = workspace / "repo"
        clone_depth = max(n_prs * 10, 500)
        print(f"[pr-audit] cloning {resolved_url} (branch={branch}, depth={clone_depth}) ...", flush=True)
        success, branch = clone_for_audit(resolved_url, branch, repo_path, depth=clone_depth)
        if not success:
            _write_pr_audit_reports(workspace, {
                "repo": label, "branch": branch, "prs_analyzed": 0,
                "focus": focus, "spec_gap_count": 0, "design_gap_count": 0,
                "skill_gap_count": 0, "practice_gap_count": 0, "findings": [],
            })
            sys.exit(1)
        # Re-extract label from actual remote URL
        try:
            res = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_path, capture_output=True, text=True, timeout=10,
            )
            remote_url = res.stdout.strip()
            if remote_url:
                label = compute_repo_label(config, remote_url)
        except Exception:
            pass
        # Write branch.txt for downstream (create_skill_pr.py)
        (workspace / "branch.txt").write_text(branch, encoding="utf-8")
        print(f"::add-task-context PR_AUDIT_REPO::{label}", flush=True)
        print(f"::add-task-context PR_AUDIT_BRANCH::{branch}", flush=True)
    elif codebase_dir and (Path(codebase_dir) / ".git").exists():
        repo_path = Path(codebase_dir)
    else:
        print("[pr-audit] no repo URL and no CODEBASE_DIR with .git -- cannot audit", file=sys.stderr, flush=True)
        _write_pr_audit_reports(workspace, {
            "repo": label, "branch": branch, "prs_analyzed": 0,
            "focus": focus, "spec_gap_count": 0, "design_gap_count": 0,
            "skill_gap_count": 0, "practice_gap_count": 0, "findings": [],
        })
        sys.exit(1)

    try:
        # Apply project-specific skill overrides
        applied = apply_project_skills(repo_path)
        if applied:
            print(f"::add-task-context REPO_SKILLS_COUNT::{applied}", flush=True)

        # -- Fetch and enrich PRs -----------------------------------------------
        print("[pr-audit] fetching merged PRs ...", flush=True)
        if all_pr_urls:
            # Specific PR URLs provided — override tracker and fetch exactly those PRs
            parsed_urls = []
            for u in all_pr_urls:
                try:
                    tracker_type, pr_num = parse_pr_url(u)
                    parsed_urls.append((tracker_type, pr_num))
                except ValueError as e:
                    print(f"[pr-audit] WARNING: skipping invalid PR URL {u!r}: {e}", flush=True)
            if parsed_urls:
                tracker_types = {t for t, _ in parsed_urls}
                if len(tracker_types) > 1:
                    print(f"[pr-audit] ERROR: mixed tracker types in PR URLs: {tracker_types}", file=sys.stderr, flush=True)
                    sys.exit(1)
                # Only override DEFAULT_TRACKER when the user did not explicitly pass
                # --tracker — an explicit flag has highest priority and must not be
                # overwritten by URL-derived tracker detection.
                if not slack_flags.get("tracker"):
                    config["DEFAULT_TRACKER"] = tracker_types.pop()
                else:
                    tracker_types.discard(config["DEFAULT_TRACKER"])  # consume without reassigning
                pr_numbers = [num for _, num in parsed_urls]
                prs = fetch_prs_by_numbers(config, pr_numbers)
                print(f"[pr-audit] fetched {len(prs)} specific PRs from URLs", flush=True)
            else:
                prs = []
        else:
            prs = fetch_prs(config, n_prs)

        # Enrich with issue linking and details
        for pr in prs:
            linked = link_pr_to_issue(pr, config)
            if linked:
                pr["linked_issue"] = linked
                details = fetch_issue_details(linked, config)
                if details:
                    linked["details"] = details

        # A.1: flush raw issue cache to artifact
        write_issues_raw()

        pr_context = build_pr_context(prs, max_chars=150_000)
        pr_ids_str = ",".join(str(pr.get("number", "")) for pr in prs if pr.get("number"))
        print(f"[pr-audit] PR context: {len(pr_context)} chars from {len(prs)} PRs", flush=True)
        print(f"::add-task-context PR_AUDIT_PR_IDS::{pr_ids_str}", flush=True)

        # Compute date range from merged_at timestamps
        merged_dates = sorted(
            d for pr in prs
            if (d := pr.get("merged_at", ""))
        )
        date_from = merged_dates[0][:10] if merged_dates else ""
        date_to = merged_dates[-1][:10] if merged_dates else ""
        jiras_reviewed = config.get("_jira_issues_reviewed", 0)
        if date_from:
            print(f"::add-task-context PR_AUDIT_DATE_FROM::{date_from}", flush=True)
            print(f"::add-task-context PR_AUDIT_DATE_TO::{date_to}", flush=True)
        if jiras_reviewed:
            print(f"::add-task-context PR_AUDIT_JIRAS_REVIEWED::{jiras_reviewed}", flush=True)

        # -- Load PR audit skill -----------------------------------------------
        _pr_audit_skill_candidates = ["pr-audit", skill, "ygs-pr-audit"]
        skill_md: str | None = None
        actual_skill = skill
        for candidate in _pr_audit_skill_candidates:
            skill_md = _load_skill_md(candidate)
            if skill_md:
                actual_skill = candidate
                break

        print(f"::add-task-context SKILL::{actual_skill}", flush=True)
        print(f"::add-task-context SKILL_LOADED::{'yes' if skill_md else 'no'}", flush=True)

        # -- Build prompt ------------------------------------------------------
        if skill_md:
            state_summary = compute_pr_state_summary(prs)
            state_summary_text = (
                f"merged={state_summary['merged']}  open={state_summary['open']}  "
                f"declined={state_summary['declined']}  total={state_summary['total']}"
            )
            # Size bucket distribution
            bucket_counts = Counter(pr.get("size_bucket") or size_bucket(pr.get("additions", 0), pr.get("deletions", 0)) for pr in prs)
            size_summary_text = (
                f"xs={bucket_counts.get('xs',0)} s={bucket_counts.get('s',0)} "
                f"m={bucket_counts.get('m',0)} l={bucket_counts.get('l',0)} xl={bucket_counts.get('xl',0)}"
            )
            state_summary_text += f"\nsize_buckets: {size_summary_text}"
            prompt = _PR_AUDIT_PROMPT_TEMPLATE.format(
                repo_label=label,
                branch=branch,
                n_prs=len(prs),
                date_from=date_from or "N/A",
                date_to=date_to or "N/A",
                jiras_reviewed=jiras_reviewed,
                focus=focus,
                pr_context=pr_context,
                skill_instructions=skill_md,
                pr_ids=pr_ids_str,
                pr_state_summary=state_summary_text,
            )
        else:
            print("[pr-audit] WARNING: no SKILL.md found -- using fallback prompt", flush=True)
            prompt = _FALLBACK_PROMPT.format(n_prs=len(prs))

        (logs_dir / "pr_audit.prompt.txt").write_text(prompt, encoding="utf-8")

        # Symlink repo_path/reports -> workspace/reports
        repo_reports_link = repo_path / "reports"
        if not repo_reports_link.exists():
            repo_reports_link.symlink_to(reports_dir.resolve())

        model = config.get("AI_MODEL")
        max_turns = int(config.get("MAX_TURNS_AUDIT", "120"))
        log_path = logs_dir / "pr_audit.log"

        print(f"[pr-audit] running with model={model} max_turns={max_turns}", flush=True)

        try:
            result = run_claude(
                prompt,
                working_dir=repo_path,
                model=model,
                max_turns=max_turns,
                log_file=log_path,
                allowed_tools="Bash,Read,Write,Edit,Glob,Grep,LS,Skill",
                system_prompt=SYSTEM_PROMPTS.get("pr_audit", SYSTEM_PROMPTS["review"]),
                primary_skill=actual_skill,
            )
        except RuntimeError as e:
            print(f"ERROR: claude failed: {e}", file=sys.stderr, flush=True)
            _write_pr_audit_reports(workspace, {
                "repo": label, "branch": branch, "prs_analyzed": len(prs),
                "focus": focus, "spec_gap_count": 0, "design_gap_count": 0,
                "skill_gap_count": 0, "practice_gap_count": 0, "findings": [],
            })
            sys.exit(1)

        status_data: dict = result.status_json or {"status": result.status}
        (reports_dir / "result.json").write_text(json.dumps(status_data, indent=2), encoding="utf-8")

        # If Claude didn't write pr_audit_report.md, recover from stdout or log
        report_md_path = reports_dir / "pr_audit_report.md"
        if not report_md_path.exists() or report_md_path.stat().st_size < 300:
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
                    f"# PR Audit -- {label} @ {branch}\n\n{analysis_text}\n",
                    encoding="utf-8",
                )
                print(f"[pr-audit] saved fallback pr_audit_report.md ({len(analysis_text)} chars)", flush=True)

        findings_path = reports_dir / "pr_audit_findings.json"
        _write_pr_audit_reports(workspace, {
            "repo": label, "branch": branch, "prs_analyzed": len(prs),
            "focus": focus,
            "spec_gap_count": status_data.get("spec_gap_count", 0),
            "design_gap_count": status_data.get("design_gap_count", 0),
            "skill_gap_count": status_data.get("skill_gap_count", 0),
            "practice_gap_count": status_data.get("practice_gap_count", 0),
            "findings": [],
        })

        # Patch Claude's JSON with identity and date fields if absent
        if findings_path.exists():
            try:
                fdata = json.loads(findings_path.read_text(encoding="utf-8"))
                patched = False
                for key, val in [
                    ("repo", label), ("branch", branch),
                    ("prs_analyzed", len(prs)), ("focus", focus),
                    ("pr_ids", pr_ids_str),
                    ("date_from", date_from), ("date_to", date_to),
                    ("jiras_reviewed", jiras_reviewed),
                ]:
                    if not fdata.get(key):
                        fdata[key] = val
                        patched = True
                if patched:
                    findings_path.write_text(json.dumps(fdata, indent=2), encoding="utf-8")
                    print("[pr-audit] patched pr_audit_findings.json with identity fields", flush=True)
            except Exception as e:
                print(f"[pr-audit] could not patch findings JSON: {e}", flush=True)

        if findings_path.exists():
            _emit_finding_counts(findings_path, fallback_repo=label, fallback_branch=branch)
        else:
            print(f"::add-task-context PR_AUDIT_SPEC_GAPS::{status_data.get('spec_gap_count', 0)}", flush=True)
            print(f"::add-task-context PR_AUDIT_DESIGN_GAPS::{status_data.get('design_gap_count', 0)}", flush=True)
            print(f"::add-task-context PR_AUDIT_SKILL_GAPS::{status_data.get('skill_gap_count', 0)}", flush=True)
            print(f"::add-task-context PR_AUDIT_PRACTICE_GAPS::{status_data.get('practice_gap_count', 0)}", flush=True)

        # Generate HTML report from Markdown
        report_md_path = reports_dir / "pr_audit_report.md"
        report_html_path = reports_dir / "pr_audit_report.html"
        if report_md_path.exists() and not report_html_path.exists():
            try:
                md_text = report_md_path.read_text(encoding="utf-8")
                html_text = render_simple_html(f"PR Audit -- {label}", md_text)
                report_html_path.write_text(html_text, encoding="utf-8")
                print(f"[pr-audit] wrote reports/pr_audit_report.html ({len(html_text)} chars)", flush=True)
            except Exception as e:
                print(f"[pr-audit] WARNING: could not render HTML: {e}", flush=True)

        print(f"::add-task-context SELECTED_MODEL::{model or ''}", flush=True)

        status_val = status_data.get("status", "")
        summary = status_data.get("summary", "")
        print(f"[pr-audit] status={status_val} spec_gaps={status_data.get('spec_gap_count', 0)} "
              f"design_gaps={status_data.get('design_gap_count', 0)} "
              f"skill_gaps={status_data.get('skill_gap_count', 0)} "
              f"practice_gaps={status_data.get('practice_gap_count', 0)}", flush=True)
        if summary:
            print(f"[pr-audit] summary: {summary}", flush=True)

        if status_val in ("DONE", "MAX_TURNS_REACHED"):
            sys.exit(0)

        print(f"ERROR: unexpected pr-audit status '{status_val}'", file=sys.stderr, flush=True)
        sys.exit(1)

    except Exception as e:
        print(f"ERROR: pr-audit failed: {e}", file=sys.stderr, flush=True)
        _write_pr_audit_reports(workspace, {
            "repo": label, "branch": branch, "prs_analyzed": 0,
            "focus": focus, "spec_gap_count": 0, "design_gap_count": 0,
            "skill_gap_count": 0, "practice_gap_count": 0, "findings": [],
        })
        sys.exit(1)


if __name__ == "__main__":
    main()
