"""Create a PR against you-got-skills with recommendations having 3+ PR evidence.

Reads reports/skill_improvements.json written by run_pr_audit.py and filters
ygs_recommendations entries where pr_evidence_count >= 3. Creates a PR against
https://github.com/bhatti/you-got-skills with targeted specialist file edits.

Only recommendations with evidence from 3+ distinct PRs are included to prevent
noisy suggestions from single-incident observations.

Usage:
    python -m scripts.analyze.create_ygs_pr

Reads:
    /workspace/reports/skill_improvements.json

Writes:
    /workspace/ygs_pr.json   (via artifacts module; issue-id "ygs-pr-audit")
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
from pathlib import Path

from scripts.common.artifacts import write_json as artifacts_write_json
from scripts.common.config import get_workspace_dir, load_config
from scripts.common.git_utils import configure_git, push_branch
from scripts.common.shell import run_cmd
from scripts.standup.slack_client import post_message

_ISSUE_ID = "ygs-pr-audit"
_YGS_REPO_URL = "https://github.com/bhatti/you-got-skills.git"
_YGS_GITHUB_REPO = "bhatti/you-got-skills"
_YGS_BASE_BRANCH = "main"


def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)
    reports_dir = workspace_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Read skill_improvements.json
    improvements_path = reports_dir / "skill_improvements.json"
    if not improvements_path.exists():
        print("[create-ygs-pr] No skill_improvements.json found", flush=True)
        _write_skipped_json(config, "no skill_improvements.json")
        print("::add-task-context YGS_PR_CREATED::no", flush=True)
        return

    try:
        improvements = json.loads(improvements_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[create-ygs-pr] Could not parse skill_improvements.json: {e}", flush=True)
        _write_skipped_json(config, f"parse error: {e}")
        print("::add-task-context YGS_PR_CREATED::no", flush=True)
        return

    ygs_recs = improvements.get("ygs_recommendations", [])

    # Filter: only include recommendations with evidence from 3+ distinct PRs
    qualified = [r for r in ygs_recs if r.get("pr_evidence_count", 0) >= 3]

    if not qualified:
        msg = f"insufficient evidence (<3 PRs) for all {len(ygs_recs)} recommendations"
        print(f"[create-ygs-pr] {msg}", flush=True)
        _write_skipped_json(config, msg)
        print("::add-task-context YGS_PR_CREATED::no", flush=True)
        return

    print(f"[create-ygs-pr] {len(qualified)}/{len(ygs_recs)} recommendations qualify (3+ PR evidence)", flush=True)

    # Derive source repo label for branch naming
    tracker = (config.get("DEFAULT_TRACKER") or "").lower().strip()
    if tracker in ("jira", "bitbucket", "jira/bitbucket"):
        source_repo = config.get("BITBUCKET_REPO", "unknown")
    else:
        source_repo = config.get("GH_REPO", "unknown")

    # Idempotency: skip if PR was already created
    existing = _read_ygs_pr_json(config)
    if existing and existing.get("url"):
        print(f"[create-ygs-pr] PR already exists: {existing['url']} — skipping", flush=True)
        print("::add-task-context YGS_PR_CREATED::yes", flush=True)
        print(f"::add-task-context YGS_PR_URL::{existing['url']}", flush=True)
        return

    # Clone you-got-skills (always GitHub, never Bitbucket)
    ygs_dir = workspace_dir / "you-got-skills"
    if not (ygs_dir / ".git").exists():
        print("[create-ygs-pr] Cloning you-got-skills...", flush=True)
        ok = _clone_ygs(ygs_dir)
        if not ok:
            print("[create-ygs-pr] Clone failed — cannot create PR", file=sys.stderr, flush=True)
            _write_skipped_json(config, "clone of you-got-skills failed")
            sys.exit(1)

    # Configure git identity
    git_name = config.get("GIT_USER_NAME", "AI Agent")
    git_email = config.get("GIT_USER_EMAIL", "ai-agent@noreply.local")
    configure_git(ygs_dir, git_name, git_email)

    # Create branch
    branch_suffix = secrets.token_hex(4)
    # Sanitize source_repo for branch name (no slashes)
    repo_slug = re.sub(r"[^a-zA-Z0-9._-]", "-", source_repo)[:30]
    pr_branch = f"ai/pr-audit-ygs-{repo_slug}-{branch_suffix}"
    _run_git(ygs_dir, ["checkout", "-b", pr_branch])

    # Apply changes — each recommendation targets a specialist file
    changes_made: list[str] = []
    for rec in qualified:
        skill_name = rec.get("skill_name", "")
        gap = rec.get("gap", "")
        suggestion = rec.get("suggestion", "")
        section = rec.get("section", "")
        file_path = rec.get("file_path", f"skills/{skill_name}/SKILL.md")
        pr_count = rec.get("pr_evidence_count", 0)

        target = ygs_dir / file_path
        # Try to locate the file if it doesn't exist at the given path
        if not target.exists():
            fname = Path(file_path).name
            candidates = list(ygs_dir.glob(f"**/{fname}"))
            # Prefer candidates whose path contains the skill name
            if candidates:
                skill_candidates = [c for c in candidates if skill_name and skill_name in str(c)]
                target = skill_candidates[0] if skill_candidates else candidates[0]

        if target.exists():
            try:
                existing_text = target.read_text(encoding="utf-8")
                addition = (
                    f"\n\n<!-- pr-audit-improvement: {source_repo} | evidence: {pr_count} PRs -->\n"
                    f"### [{gap[:80]}]\n\n"
                )
                if section:
                    addition += f"*Section:* {section}\n\n"
                addition += f"{suggestion}\n"
                target.write_text(existing_text + addition, encoding="utf-8")
                rel = str(target.relative_to(ygs_dir))
                changes_made.append(rel)
                print(f"[create-ygs-pr] updated {rel}", flush=True)
            except OSError as e:
                print(f"[create-ygs-pr] Could not write {file_path}: {e}", flush=True)
        else:
            print(f"[create-ygs-pr] target not found: {file_path} — skipping", flush=True)

    if not changes_made:
        print("[create-ygs-pr] No target skill files found in you-got-skills — skipping", flush=True)
        _run_git(ygs_dir, ["checkout", _YGS_BASE_BRANCH])
        _write_skipped_json(config, "no target skill files found in you-got-skills")
        print("::add-task-context YGS_PR_CREATED::no", flush=True)
        return

    # Commit + push
    _run_git(ygs_dir, ["add", "-A"])
    commit_msg = (
        f"pr-audit({source_repo}): improve {len(changes_made)} ygs skills\n\n"
        f"Evidence from {len(qualified)} recommendations with 3+ PR evidence.\n"
        f"Source repo: {source_repo}\n\n"
        "Co-Authored-By: Claude <noreply@anthropic.com>"
    )
    _run_git(ygs_dir, ["commit", "-m", commit_msg])

    gh_token = config.get("GH_TOKEN", "")
    try:
        push_branch(
            ygs_dir, pr_branch,
            http_token=gh_token,
            http_username="x-access-token",
            url=_YGS_REPO_URL,
        )
    except Exception as e:
        stderr_detail = (e.stderr + e.output) if isinstance(e, subprocess.CalledProcessError) else str(e)
        print(f"[create-ygs-pr] Push failed: {stderr_detail[:500]}", file=sys.stderr, flush=True)
        _write_skipped_json(config, f"push failed: {str(e)[:200]}")
        sys.exit(1)

    # Create PR via gh CLI (always GitHub)
    title = f"[AI] pr-audit({source_repo}): improve {len(changes_made)} ygs skills"
    body = _build_pr_body(qualified, source_repo, reports_dir, changes_made)

    result = run_cmd(
        ["gh", "pr", "create", "-R", _YGS_GITHUB_REPO,
         "--title", title, "--body", body, "--head", pr_branch],
        check=False,
    )
    if result.returncode != 0:
        print(f"[create-ygs-pr] gh pr create failed: {result.stderr.strip()[:300]}",
              file=sys.stderr, flush=True)
        _write_skipped_json(config, "gh pr create failed")
        sys.exit(1)

    m = re.search(r'https://github\.com/[^\s]+/pull/(\d+)', result.stdout)
    pr_url = m.group(0) if m else result.stdout.strip()
    pr_num = int(m.group(1)) if m else 0

    ygs_pr_data = {
        "status": "created",
        "url": pr_url,
        "number": pr_num,
        "branch": pr_branch,
        "repo": "you-got-skills",
        "tracker": "github",
        "source_repo": source_repo,
        "changes_made": len(changes_made),
    }
    artifacts_write_json(config, _ISSUE_ID, "ygs_pr.json", ygs_pr_data)

    print(f"::add-task-context YGS_PR_CREATED::yes", flush=True)
    if pr_url:
        print(f"::add-task-context YGS_PR_URL::{pr_url}", flush=True)
    if pr_num:
        print(f"::add-task-context YGS_PR_NUMBER::{pr_num}", flush=True)

    # Post to Slack
    try:
        post_message(
            config,
            f":books: *YGS Skill Improvements* — <{pr_url}|PR #{pr_num}>\n"
            f"{len(changes_made)} skill file updates from {source_repo} audit "
            f"({len(qualified)} recommendations with 3+ PR evidence)",
            thread_ts=config.get("SLACK_THREAD_TS", ""),
        )
    except Exception as e:
        print(f"[create-ygs-pr] Slack post failed: {e}", flush=True)

    print(f"[create-ygs-pr] PR created: {pr_url}", flush=True)


# -- Helpers -------------------------------------------------------------------

def _clone_ygs(dest: Path) -> bool:
    """Shallow-clone you-got-skills repo."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", _YGS_REPO_URL, str(dest)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            print(f"[create-ygs-pr] git clone failed: {result.stderr.strip()[:300]}",
                  file=sys.stderr, flush=True)
            return False
        return True
    except subprocess.TimeoutExpired:
        print("[create-ygs-pr] git clone timed out", file=sys.stderr, flush=True)
        return False
    except Exception as e:
        print(f"[create-ygs-pr] git clone exception: {e}", file=sys.stderr, flush=True)
        return False


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess:
    """Run a git command in the given directory."""
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"[create-ygs-pr] git {' '.join(args)} failed: {result.stderr.strip()[:300]}",
                  file=sys.stderr, flush=True)
        return result
    except subprocess.TimeoutExpired:
        print(f"[create-ygs-pr] git {' '.join(args)} timed out", file=sys.stderr, flush=True)
        raise


def _build_pr_body(
    qualified: list[dict],
    source_repo: str,
    reports_dir: Path,
    changes_made: list[str],
) -> str:
    """Build PR body with recommendation table and audit context."""
    lines = [
        f"## PR Audit: you-got-skills Improvements from `{source_repo}`",
        "",
        f"Based on analyzing merged PRs in **{source_repo}**, this PR proposes improvements "
        f"to {len(changes_made)} shared skill files.",
        "",
        "> Only recommendations with evidence from **3+ distinct PRs** are included — "
        "single-incident observations are excluded to prevent noise.",
        "",
        "## Recommendations",
        "",
        "| Skill | Gap | Evidence (PRs) | Section |",
        "|-------|-----|---------------|---------|",
    ]
    for rec in qualified:
        skill = rec.get("skill_name", "?")
        gap = rec.get("gap", "?")[:80]
        pr_count = rec.get("pr_evidence_count", 0)
        section = rec.get("section", "—")
        lines.append(f"| {skill} | {gap} | {pr_count} PRs | {section} |")

    lines.extend(["", "## Changed Files", ""])
    for f in changes_made:
        lines.append(f"- `{f}`")

    # Include summary from audit report if available
    audit_report_path = reports_dir / "pr_audit_report.md"
    if audit_report_path.exists():
        try:
            report_text = audit_report_path.read_text(encoding="utf-8")
            lines.extend([
                "",
                "<details><summary>Source Audit Report (excerpt)</summary>",
                "",
                report_text[:4000],
            ])
            if len(report_text) > 4000:
                lines.append("\n_... (truncated — see full audit report artifact)_")
            lines.extend(["", "</details>", ""])
        except OSError:
            pass

    lines.append("_This PR was created by an AI agent based on PR audit findings._")
    return "\n".join(lines)


def _write_skipped_json(config: dict, reason: str) -> None:
    """Write ygs_pr.json indicating the PR was skipped (not an error)."""
    artifacts_write_json(
        config, _ISSUE_ID, "ygs_pr.json",
        {"status": "skipped", "reason": reason, "url": "", "number": 0},
    )


def _read_ygs_pr_json(config: dict) -> dict | None:
    """Read existing ygs_pr.json if present."""
    try:
        from scripts.common.artifacts import read_json as artifacts_read_json
        return artifacts_read_json(config, _ISSUE_ID, "ygs_pr.json")
    except Exception:
        return None


if __name__ == "__main__":
    main()
