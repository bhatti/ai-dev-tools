"""Run a PR review or self-review using a ygs skill via Claude Code.

Usage:
    # PR review (external)
    python -m scripts.review.run --pr-url <url> [--skill ygs-review-pr]

    # Self-review on a local diff (post-implement, pre-PR)
    python -m scripts.review.run --mode self-review --issue-id <id> [--base-branch main]

Required env: ANTHROPIC_API_KEY or CLAUDE_CODE_USE_BEDROCK=1

Writes (PR review):   /workspace/review_result.json, findings.json, logs/review.*
Writes (self-review): /workspace/<issue_id>/self_review.json, logs/self_review.*

Exit codes: 0=done, 1=error, 2=BLOCKED (self-review only — critical findings)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from scripts.common.artifacts import read_text
from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS, _ensure_ygs_skills
from scripts.common.config import get_workspace_dir, get_issue_dir, load_config, validate_claude_config
from scripts.common.skills import apply_project_skills
from scripts.review.post_findings import render_report_md, render_report_html


# Skill search paths — mirrors scripts/adhoc/run_skill.py _skill_search_paths()
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
            print(f"[review] skill path: {candidate}", flush=True)
            return candidate.read_text(encoding="utf-8")
    return None


REVIEW_PROMPT_TEMPLATE = """\
## PR URL

{pr_url}

## Review Instructions

{skill_instructions}

---

After completing the review per the instructions above, write your findings to
`findings.json` in the current working directory using this exact structure:

```json
{{
  "pr_url": "{pr_url}",
  "verdict": "APPROVE | REQUEST_CHANGES | COMMENT",
  "findings": [
    {{
      "severity": "CRITICAL | HIGH | MEDIUM | LOW",
      "confidence": "HIGH | MEDIUM | LOW",
      "title": "<short one-line title>",
      "file": "<path/to/file or empty string>",
      "line": null,
      "domain": "correctness | security | api | sre",
      "description": "<what is wrong and why it matters>",
      "fix": "<concrete suggested fix>"
    }}
  ],
  "summary": "<one sentence overall assessment>"
}}
```

If `findings.json` already exists from a prior step, do not overwrite it.

Output ONLY this JSON on the last line (no text after it):
{{"status":"DONE","findings_count":<N>,"verdict":"<APPROVE|REQUEST_CHANGES|COMMENT>","summary":"<one sentence>"}}

Or if something went wrong:
{{"status":"ERROR","reason":"<explanation>"}}
"""


SELF_REVIEW_PROMPT_TEMPLATE = """\
You are performing a self-review of the changes you just implemented.

## Self-Review Instructions

{skill_instructions}

---

Run `git diff {base_branch}...HEAD` to get the full diff of your changes.

Review the diff against the criteria above. Focus on:
- Correctness: does the code do what was intended?
- Failure modes: partial failure, concurrent access, edge cases
- Code hygiene: debug code, TODOs, unused imports/variables, scope creep
- Naming and consistency with the surrounding codebase

Write your findings to `self_review.json` in the current working directory:
```json
{{
  "status": "APPROVED",
  "findings": [],
  "notes": ""
}}
```
Where status is:
- "APPROVED" — no CRITICAL or HIGH findings (safe to create PR)
- "NEEDS_FIX" — HIGH findings that you should fix before proceeding
- "BLOCKED" — CRITICAL findings that require human review before creating a PR

If status is NEEDS_FIX: fix the issues, re-run the test suite, then update self_review.json to APPROVED.
If status is BLOCKED: document why in `notes` and do NOT attempt to fix (needs human judgment).

Output ONLY this JSON on the last line (no text after it):
{{"status":"DONE","self_review_status":"<APPROVED|NEEDS_FIX|BLOCKED>","findings_count":<N>,"notes":"<summary>"}}
"""


REVIEW_PROMPT_FALLBACK = """\
## PR URL

{pr_url}

## Instructions

Review the pull request at the URL above. Fetch the diff and description using the
Bash tool with curl (do NOT use WebFetch — it does not support authentication for
private repositories and will return 404 or 403).

For Bitbucket PRs, run these Bash commands:
  PR_META=$(curl -sf "https://api.bitbucket.org/2.0/repositories/$BITBUCKET_WORKSPACE/$BITBUCKET_REPO/pullrequests/{bb_pr_id}" -u "$BITBUCKET_USERNAME:$BITBUCKET_TOKEN")
  PR_DIFF=$(curl -sf "https://api.bitbucket.org/2.0/repositories/$BITBUCKET_WORKSPACE/$BITBUCKET_REPO/pullrequests/{bb_pr_id}/diff" -u "$BITBUCKET_USERNAME:$BITBUCKET_TOKEN")

For GitHub PRs, run:
  gh pr view <number> --repo <owner/repo> --json title,body,changedFiles
  gh pr diff <number> --repo <owner/repo>

The PR URL is: {pr_url}
For Bitbucket: workspace=$BITBUCKET_WORKSPACE repo=$BITBUCKET_REPO

After fetching, perform a code review covering: correctness, security, API surface,
and SRE concerns.

Write findings to `findings.json` with this structure:
{{
  "pr_url": "{pr_url}",
  "verdict": "APPROVE | REQUEST_CHANGES | COMMENT",
  "findings": [{{"severity":"HIGH","confidence":"HIGH","title":"...","file":"","line":null,"domain":"correctness","description":"...","fix":"..."}}],
  "summary": "one sentence"
}}

Output ONLY this JSON on the last line:
{{"status":"DONE","findings_count":<N>,"verdict":"<verdict>","summary":"<one sentence>"}}
"""


@click.command()
@click.option("--pr-url", default=None, help="Full URL or number of the PR to review")
@click.option("--skill", default="ygs-review-pr", show_default=True, help="Skill name to load")
@click.option("--issue-id", default=None, help="Issue ID (required for --mode self-review)")
@click.option("--mode", default="review", type=click.Choice(["review", "self-review"]), show_default=True,
              help="'review' = PR review; 'self-review' = diff review against local branch")
@click.option("--base-branch", default=None, help="Base branch for self-review diff (falls back to BASE_BRANCH env)")
def main(pr_url: str | None, skill: str, issue_id: str | None, mode: str, base_branch: str | None) -> None:
    config = load_config()
    validate_claude_config(config)

    if mode == "self-review":
        _run_self_review(config, issue_id, skill, base_branch)
    else:
        if not pr_url:
            print("ERROR: --pr-url is required for review mode", file=sys.stderr, flush=True)
            sys.exit(1)
        _run_pr_review(config, pr_url, skill)


def _clone_repo_skills(pr_url: str, workspace: Path) -> None:
    """Sparse-clone only .claude/skills/ from the PR repo into workspace/repo."""
    import re as _re
    import subprocess
    bb = _re.match(r'https://bitbucket\.org/([^/]+)/([^/]+)/pull-requests?/\d+', pr_url)
    gh = _re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/\d+', pr_url)
    if bb:
        token = os.environ.get("BITBUCKET_TOKEN", os.environ.get("BITBUCKET_APP_PASSWORD", ""))
        if token:
            # ATATT* = Bitbucket access token → x-token-auth; otherwise app password → user:pass
            if token.startswith("ATATT"):
                auth = f"x-token-auth:{token}@"
            else:
                user = os.environ.get("BITBUCKET_USERNAME", "")
                auth = f"{user}:{token}@" if user else f"x-token-auth:{token}@"
        else:
            auth = ""
        clone_url = f"https://{auth}bitbucket.org/{bb.group(1)}/{bb.group(2)}.git"
    elif gh:
        token = os.environ.get("GH_TOKEN", os.environ.get("GITHUB_TOKEN", ""))
        auth = f"x-token-auth:{token}@" if token else ""
        clone_url = f"https://{auth}github.com/{gh.group(1)}/{gh.group(2)}.git"
    else:
        print("[review] unrecognized PR URL format — skipping repo skills clone", flush=True)
        return
    dest = workspace / "repo"
    if (dest / ".git").exists():
        print("[review] repo already cloned — skipping sparse-checkout", flush=True)
        return
    dest.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "--no-checkout", "--depth", "1", "--filter=blob:none",
             clone_url, str(dest)],
            capture_output=True, timeout=60, check=True,
        )
        subprocess.run(
            ["git", "-C", str(dest), "sparse-checkout", "set", ".claude/skills"],
            capture_output=True, timeout=10, check=True,
        )
        subprocess.run(
            ["git", "-C", str(dest), "checkout"],
            capture_output=True, timeout=30, check=True,
        )
        skills = dest / ".claude" / "skills"
        if skills.exists():
            skill_names = [p.name for p in skills.iterdir() if p.is_dir()]
            print(f"[review] repo skills cloned: {skill_names}", flush=True)
        else:
            print("[review] no .claude/skills in this repo", flush=True)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace")[:200] if e.stderr else ""
        print(f"[review] repo skills clone failed (non-fatal): {stderr}", flush=True)
    except Exception as e:
        print(f"[review] repo skills clone error (non-fatal): {e}", flush=True)


def _run_pr_review(config: dict, pr_url: str, skill: str) -> None:
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)
    logs_dir = workspace / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    print(f"[review] pr_url={pr_url} skill={skill}", flush=True)

    _clone_repo_skills(pr_url, workspace)
    _ensure_ygs_skills()

    # Symlink repo skills into ~/.claude/skills/ so Claude can invoke them with /skill-name
    repo_dir = workspace / "repo"
    applied = apply_project_skills(repo_dir)
    if applied:
        print(f"::add-task-context REPO_SKILLS_COUNT::{applied}", flush=True)

    # Prefer repo-specific review skill over the default ygs skill
    _review_skill_candidates = ["review-pr", "goatbot-pr-review", skill]
    skill_md = None
    actual_skill = skill
    for candidate in _review_skill_candidates:
        skill_md = _load_skill_md(candidate)
        if skill_md:
            actual_skill = candidate
            break

    print(f"::add-task-context SKILL::{actual_skill}", flush=True)
    print(f"::add-task-context SKILL_LOADED::{'yes' if skill_md else 'no'}", flush=True)
    if skill_md:
        print(f"[review] loaded {actual_skill} SKILL.md ({len(skill_md)} chars)", flush=True)
        prompt = REVIEW_PROMPT_TEMPLATE.format(
            pr_url=pr_url,
            skill_instructions=skill_md,
        )
    else:
        print(f"[review] WARNING: {actual_skill}/SKILL.md not found — using fallback instructions", flush=True)
        import re as _re
        _bb_match = _re.search(r"/pull-requests?/(\d+)", pr_url)
        _bb_pr_id = _bb_match.group(1) if _bb_match else "<PR_ID>"
        prompt = REVIEW_PROMPT_FALLBACK.format(pr_url=pr_url, bb_pr_id=_bb_pr_id)

    prompt_path = logs_dir / "review.prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    model = config.get("AI_MODEL")
    max_turns = int(config.get("MAX_TURNS_REVIEW", "100"))
    log_path = logs_dir / "review.log"

    print(f"[review] Running review with model={model}, max_turns={max_turns}", flush=True)
    try:
        result = run_claude(
            prompt,
            working_dir=workspace,
            model=model,
            max_turns=max_turns,
            log_file=log_path,
            allowed_tools="Bash,Read,Write,Edit,Glob,Grep,LS,Skill",
            system_prompt=SYSTEM_PROMPTS["review"],
        )
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr, flush=True)
        _write_json(workspace / "review_result.json", {"status": "ERROR", "reason": str(e)})
        _ensure_findings_stub(workspace / "findings.json", pr_url)
        sys.exit(1)

    status_data: dict = result.status_json or {"status": result.status}
    _write_json(workspace / "review_result.json", status_data)

    findings_path = workspace / "findings.json"
    if not findings_path.exists():
        verdict = status_data.get("verdict", "COMMENT")
        summary = status_data.get("summary", "Review completed.")
        _write_json(findings_path, {
            "pr_url": pr_url,
            "verdict": verdict,
            "findings": [],
            "summary": summary,
        })

    findings_count = status_data.get("findings_count", 0)
    verdict = status_data.get("verdict", "COMMENT")
    summary = status_data.get("summary", "")

    print(f"[review] status={status_data.get('status')} findings={findings_count} verdict={verdict}", flush=True)
    print(f"[review] summary: {summary}", flush=True)

    try:
        findings = json.loads(findings_path.read_text(encoding="utf-8"))
        md_text = render_report_md(findings)
        html_text = render_report_html(findings, md_text)
        reports_dir = workspace / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "report.md").write_text(md_text, encoding="utf-8")
        (reports_dir / "report.html").write_text(html_text, encoding="utf-8")
        (reports_dir / "result.json").write_text(json.dumps(status_data, indent=2), encoding="utf-8")
        (reports_dir / "findings.json").write_text(findings_path.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[review] wrote reports/report.md, reports/report.html, reports/result.json, reports/findings.json", flush=True)
    except Exception as e:
        print(f"[review] WARNING: could not render report: {e}", file=sys.stderr, flush=True)

    status_val = status_data.get("status", "")
    error_reason = status_data.get("reason", "")
    print(f"::add-task-context SELECTED_MODEL::{config.get('AI_MODEL', '')}", flush=True)
    print(f"::add-task-context FINDINGS_COUNT::{findings_count}", flush=True)
    print(f"::add-task-context REVIEW_VERDICT::{verdict}", flush=True)
    if error_reason:
        print(f"::add-task-context ERROR_REASON::{error_reason[:300]}", flush=True)
    if status_val in ("DONE", "DONE_WITH_CONCERNS", "MAX_TURNS_REACHED"):
        sys.exit(0)

    print(f"ERROR: unexpected review status '{status_val}'", file=sys.stderr, flush=True)
    sys.exit(1)


def _run_self_review(config: dict, issue_id: str | None, skill: str, base_branch: str | None) -> None:
    if not issue_id:
        print("ERROR: --issue-id is required for --mode self-review", file=sys.stderr, flush=True)
        sys.exit(1)

    issue_dir = get_issue_dir(config, issue_id)
    repo_dir = issue_dir / "repo"
    if not repo_dir.exists():
        print(f"ERROR: repo dir not found at {repo_dir} — run clone-repo first", file=sys.stderr, flush=True)
        sys.exit(1)

    logs_dir = issue_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    branch = base_branch or config.get("BASE_BRANCH") or os.environ.get("BASE_BRANCH", "main")
    review_skill = "ygs-code-review"  # self-review always uses code-review (not PR review)

    print(f"[self-review] issue={issue_id} base_branch={branch} skill={review_skill}", flush=True)

    _ensure_ygs_skills()

    skill_md = _load_skill_md(review_skill)
    print(f"::add-task-context SKILL::{review_skill}", flush=True)
    print(f"::add-task-context SKILL_LOADED::{'yes' if skill_md else 'no'}", flush=True)
    if skill_md:
        print(f"[self-review] loaded {review_skill} SKILL.md ({len(skill_md)} chars)", flush=True)
        prompt = SELF_REVIEW_PROMPT_TEMPLATE.format(
            base_branch=branch,
            skill_instructions=skill_md,
        )
    else:
        # Minimal fallback if skill not available
        prompt = SELF_REVIEW_PROMPT_TEMPLATE.format(
            base_branch=branch,
            skill_instructions=(
                "Review the diff for: correctness (logic errors, null dereference, race conditions), "
                "code hygiene (unused variables, debug code, TODOs), failure modes (partial failure, "
                "large inputs, concurrent access), and naming consistency."
            ),
        )

    prompt_path = logs_dir / "self_review.prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    model = config.get("AI_MODEL")
    max_turns = int(config.get("MAX_TURNS_REVIEW", "50"))
    log_path = logs_dir / "self_review.log"

    print(f"[self-review] model={model} max_turns={max_turns}", flush=True)
    try:
        result = run_claude(
            prompt,
            working_dir=repo_dir,
            model=model,
            max_turns=max_turns,
            log_file=log_path,
            allowed_tools="Bash,Read,Write,Edit,Glob,Grep,LS,Skill",
            system_prompt=SYSTEM_PROMPTS["review"],
        )
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr, flush=True)
        _write_json(issue_dir / "self_review.json", {"status": "ERROR", "reason": str(e)})
        sys.exit(1)

    status_data: dict = result.status_json or {"status": result.status}
    sr_status = status_data.get("self_review_status", "APPROVED")
    findings_count = status_data.get("findings_count", 0)
    notes = status_data.get("notes", "")

    # Ensure self_review.json was written by Claude; create stub if not
    sr_path = repo_dir / "self_review.json"
    if not sr_path.exists():
        _write_json(sr_path, {"status": sr_status, "findings": [], "notes": notes})

    # Copy to issue_dir for artifact collection
    import shutil
    shutil.copy2(sr_path, issue_dir / "self_review.json")

    print(f"[self-review] self_review_status={sr_status} findings={findings_count} notes={notes!r}", flush=True)

    # Exit 2 if BLOCKED — pipeline should PAUSE_JOB for human review
    if sr_status == "BLOCKED":
        print(f"[self-review] BLOCKED — critical findings require human review before PR", flush=True)
        sys.exit(2)

    sys.exit(0)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _ensure_findings_stub(path: Path, pr_url: str) -> None:
    if not path.exists():
        _write_json(path, {
            "pr_url": pr_url,
            "verdict": "COMMENT",
            "findings": [],
            "summary": "Review did not complete — see review.log for details.",
        })


if __name__ == "__main__":
    main()
