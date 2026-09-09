"""Create a PR proposing skill/doc improvements based on PR audit findings.

Reads reports/skill_improvements.json written by run_pr_audit.py and creates
a branch with the proposed changes, then opens a PR.

Usage:
    python -m scripts.analyze.create_skill_pr

Reads:
    /workspace/reports/skill_improvements.json
    /workspace/branch.txt

Writes:
    /workspace/pr.json                   (via artifacts module; get_issue_dir returns workspace root)
    /workspace/reports/ygs_recommendations.md
"""

from __future__ import annotations

import json
import re
import secrets
import subprocess
import sys
from pathlib import Path

from scripts.common import bitbucket_api
from scripts.common.artifacts import read_json as artifacts_read_json
from scripts.common.artifacts import write_json as artifacts_write_json
from scripts.common.config import get_workspace_dir, load_config
from scripts.common.git_utils import clone_by_tracker, configure_git, detect_bitbucket_url, push_branch
from scripts.common.shell import run_cmd
from scripts.standup.slack_client import post_message

_ISSUE_ID = "pr-audit"


def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)
    reports_dir = workspace_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Read skill_improvements.json
    improvements_path = reports_dir / "skill_improvements.json"
    if not improvements_path.exists():
        print("[create-skill-pr] No skill_improvements.json found", flush=True)
        _write_empty_pr_json(config)
        print("::add-task-context SKILL_PR_CREATED::no", flush=True)
        return

    try:
        improvements = json.loads(improvements_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[create-skill-pr] Could not parse skill_improvements.json: {e}", flush=True)
        _write_empty_pr_json(config)
        print("::add-task-context SKILL_PR_CREATED::no", flush=True)
        return

    repo_changes = improvements.get("repo_skill_changes", [])
    new_docs = improvements.get("new_docs", [])
    ygs_recs = improvements.get("ygs_recommendations", [])

    # Read optional plan and findings for enriched PR body
    audit_report = _read_text_safe(reports_dir / "pr_audit_report.md")
    skill_update_plan = _read_text_safe(reports_dir / "skill_update_plan.md")

    if not repo_changes and not new_docs:
        print("[create-skill-pr] No repo changes to propose", flush=True)
        _write_empty_pr_json(config)
        print("::add-task-context SKILL_PR_CREATED::no", flush=True)
        if ygs_recs:
            _write_ygs_recommendations(reports_dir, ygs_recs)
        return

    # Idempotency: skip if PR was already created (e.g. task retry)
    existing = artifacts_read_json(config, _ISSUE_ID, "pr.json")
    if existing and existing.get("url"):
        print(f"[create-skill-pr] PR already exists: {existing['url']} — skipping", flush=True)
        print("::add-task-context SKILL_PR_CREATED::yes", flush=True)
        print(f"::add-task-context PR_URL::{existing['url']}", flush=True)
        return

    # Clone the repo (each task runs in a separate pod — workspace is not shared)
    tracker = (config.get("DEFAULT_TRACKER") or "").lower().strip()
    codebase_dir = Path(config.get("CODEBASE_DIR", str(workspace_dir / "repo")))
    if not (codebase_dir / ".git").exists():
        print("[create-skill-pr] Cloning repo (separate pod, workspace not persisted)...", flush=True)
        ok = _clone_repo(config, codebase_dir, tracker)
        if not ok:
            print("[create-skill-pr] Clone failed — cannot create PR", file=sys.stderr, flush=True)
            _write_empty_pr_json(config)
            print("::add-task-context SKILL_PR_CREATED::no", flush=True)
            if ygs_recs:
                _write_ygs_recommendations(reports_dir, ygs_recs)
            sys.exit(1)

    # Read base branch — tracker-specific, no cross-fallback
    branch_file = workspace_dir / "branch.txt"
    if branch_file.exists():
        base_branch = branch_file.read_text().strip()
    elif tracker in ("jira", "bitbucket", "jira/bitbucket"):
        base_branch = config.get("BB_REPO_BRANCH", "main")
    else:
        base_branch = config.get("GH_REPO_BRANCH", "main")

    # Configure git
    git_name = config.get("GIT_USER_NAME", "AI Agent")
    git_email = config.get("GIT_USER_EMAIL", "ai-agent@noreply.local")
    configure_git(codebase_dir, git_name, git_email)

    # Create branch
    branch_suffix = secrets.token_hex(4)
    pr_branch = f"ai/pr-audit-improvements-{branch_suffix}"
    _run_git(codebase_dir, ["checkout", "-b", pr_branch])

    # Apply changes
    changes_made: list[str] = []
    for change in repo_changes:
        action = change.get("action", "update")
        file_path = change.get("file_path", "")
        content = change.get("changes", "")
        if not file_path or not content:
            continue
        target = codebase_dir / file_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if action == "create":
                target.write_text(content, encoding="utf-8")
            else:
                existing_text = target.read_text(encoding="utf-8") if target.exists() else ""
                target.write_text(existing_text + "\n" + content, encoding="utf-8")
            changes_made.append(file_path)
        except OSError as e:
            print(f"[create-skill-pr] Could not write {file_path}: {e}", flush=True)

    for doc in new_docs:
        doc_path = doc.get("path", "")
        doc_content = doc.get("content", "")
        if not doc_path or not doc_content:
            continue
        target = codebase_dir / doc_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(doc_content, encoding="utf-8")
            changes_made.append(doc_path)
        except OSError as e:
            print(f"[create-skill-pr] Could not write {doc_path}: {e}", flush=True)

    if not changes_made:
        print("[create-skill-pr] No files written -- nothing to commit", flush=True)
        _run_git(codebase_dir, ["checkout", base_branch])
        _write_empty_pr_json(config)
        print("::add-task-context SKILL_PR_CREATED::no", flush=True)
        if ygs_recs:
            _write_ygs_recommendations(reports_dir, ygs_recs)
        return

    # Commit + push (DRY: same credential logic as implement workflow's push_impl.py)
    _run_git(codebase_dir, ["add", "-A"])
    commit_msg = (
        f"pr-audit: improve skills based on {len(changes_made)} changes\n\n"
        "Co-Authored-By: Claude <noreply@anthropic.com>"
    )
    _run_git(codebase_dir, ["commit", "-m", commit_msg])
    try:
        if tracker in ("jira", "bitbucket", "jira/bitbucket"):
            workspace = config.get("BITBUCKET_WORKSPACE", "")
            repo_name = config.get("BITBUCKET_REPO", "")
            push_branch(
                codebase_dir, pr_branch,
                http_token=config.get("BITBUCKET_TOKEN", ""),
                http_username=config.get("BITBUCKET_USERNAME", "x-token-auth"),
                url=detect_bitbucket_url(workspace, repo_name, use_ssh=False),
            )
        else:
            gh_token = config.get("GH_TOKEN", "")
            gh_org = config.get("GH_ORG", "")
            gh_repo = config.get("GH_REPO", "")
            print(f"[create-skill-pr] pushing to github.com/{gh_org}/{gh_repo} "
                  f"token={'***' if gh_token else '(MISSING)'} len={len(gh_token)}", flush=True)
            push_branch(
                codebase_dir, pr_branch,
                http_token=gh_token,
                http_username="x-access-token",
                url=f"https://github.com/{gh_org}/{gh_repo}.git",
            )
    except Exception as e:
        stderr_detail = (e.stderr + e.output) if isinstance(e, subprocess.CalledProcessError) else str(e)
        print(f"[create-skill-pr] Push failed: {stderr_detail[:500]}", file=sys.stderr, flush=True)
        _write_empty_pr_json(config)
        print("::add-task-context SKILL_PR_CREATED::no", flush=True)
        if ygs_recs:
            _write_ygs_recommendations(reports_dir, ygs_recs)
        sys.exit(1)

    # Create PR — tracker-specific: GH uses gh CLI, BB uses bitbucket_api
    pr_info = _create_pr(config, pr_branch, base_branch, repo_changes, new_docs, tracker,
                         audit_report=audit_report, skill_update_plan=skill_update_plan)

    # Determine org/repo for pr.json metadata — tracker-specific, no cross-fallback
    if tracker in ("jira", "bitbucket", "jira/bitbucket"):
        org = config.get("BITBUCKET_WORKSPACE", "")
        repo = config.get("BITBUCKET_REPO", "")
    else:
        org = config.get("GH_ORG", "")
        repo = config.get("GH_REPO", "")

    pr_url = pr_info.get("url", "")
    pr_num = pr_info.get("number", 0)
    pr_json = {
        "url": pr_url,
        "number": pr_num,
        "branch": pr_branch,
        "repo": f"{org}/{repo}" if org and repo else "",
        "tracker": "bitbucket" if tracker in ("jira", "bitbucket", "jira/bitbucket") else "github",
    }
    # Write to /workspace/pr.json via artifacts module (get_issue_dir returns workspace root)
    artifacts_write_json(config, _ISSUE_ID, "pr.json", pr_json)

    # Emit context markers
    print(f"::add-task-context SKILL_PR_CREATED::{'yes' if pr_url else 'no'}", flush=True)
    if pr_url:
        print(f"::add-task-context PR_URL::{pr_url}", flush=True)
    if pr_num:
        print(f"::add-task-context PR_NUMBER::{pr_num}", flush=True)
    print(f"::add-task-context PR_BRANCH::{pr_branch}", flush=True)
    print(f"::add-task-context SKILL_CHANGES::{len(repo_changes)}", flush=True)
    print(f"::add-task-context DOC_CHANGES::{len(new_docs)}", flush=True)

    # Post to Slack
    try:
        post_message(
            config,
            f":pencil: *PR Audit Improvements* -- <{pr_url}|PR #{pr_num}>\n"
            f"{len(repo_changes)} skill updates, {len(new_docs)} new docs",
            thread_ts=config.get("SLACK_THREAD_TS", ""),
        )
    except Exception as e:
        print(f"[create-skill-pr] Slack post failed: {e}", flush=True)

    # Write ygs_recommendations.md
    if ygs_recs:
        _write_ygs_recommendations(reports_dir, ygs_recs)

    print(f"[create-skill-pr] PR created: {pr_url}", flush=True)


# -- Helpers -------------------------------------------------------------------

def _clone_repo(config: dict, dest: Path, tracker: str) -> bool:
    """Clone the repo using shared clone_by_tracker (DRY with gh/jira workflows)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        clone_by_tracker(config, dest, tracker)
        return True
    except Exception as e:
        print(f"[create-skill-pr] clone failed: {e}", file=sys.stderr, flush=True)
        return False


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess:
    """Run a git command in the given directory."""
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            print(f"[create-skill-pr] git {' '.join(args)} failed: {result.stderr.strip()[:300]}",
                  file=sys.stderr, flush=True)
        return result
    except subprocess.TimeoutExpired:
        print(f"[create-skill-pr] git {' '.join(args)} timed out", file=sys.stderr, flush=True)
        raise


def _create_pr(
    config: dict,
    head_branch: str,
    base_branch: str,
    repo_changes: list[dict],
    new_docs: list[dict],
    tracker: str,
    audit_report: str = "",
    skill_update_plan: str = "",
) -> dict:
    """Create a PR on GitHub or Bitbucket.

    Returns {'url': ..., 'number': ...}. Calls sys.exit(1) on failure.
    Uses run_cmd (GH) and bitbucket_api.create_pr (BB) — same as implement workflow (DRY).
    PR body includes audit findings summary and skill update plan for reviewer context.
    """
    title = f"[AI] PR audit: skill & doc improvements ({len(repo_changes) + len(new_docs)} changes)"
    body = _build_pr_body(repo_changes, new_docs, audit_report, skill_update_plan)

    if tracker in ("jira", "bitbucket", "jira/bitbucket"):
        return _create_bitbucket_pr(config, title, body, head_branch, base_branch)
    return _create_github_pr(config, title, body, head_branch)


def _build_pr_body(
    repo_changes: list[dict],
    new_docs: list[dict],
    audit_report: str = "",
    skill_update_plan: str = "",
) -> str:
    """Build enriched PR body with findings summary, change list, and full audit report.

    Kept in one place (DRY) so both GH and BB get identical descriptions.
    """
    # Extract first non-empty paragraph from audit report as the summary
    summary = "Automated improvements identified by PR audit analysis."
    if audit_report:
        for line in audit_report.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                summary = stripped[:400]
                break

    body_lines = ["## Summary", "", summary, ""]

    if repo_changes:
        body_lines.append(f"## Skill Updates ({len(repo_changes)} changes)")
        body_lines.append("")
        for ch in repo_changes[:15]:
            body_lines.append(f"- `{ch.get('file_path', '?')}`: {ch.get('description', '')}")
        if len(repo_changes) > 15:
            body_lines.append(f"- ...and {len(repo_changes) - 15} more")
        body_lines.append("")

    if new_docs:
        body_lines.append(f"## New Documentation ({len(new_docs)} files)")
        body_lines.append("")
        for d in new_docs[:15]:
            body_lines.append(f"- `{d.get('path', '?')}`: {d.get('description', '')}")
        if len(new_docs) > 15:
            body_lines.append(f"- ...and {len(new_docs) - 15} more")
        body_lines.append("")

    if skill_update_plan:
        body_lines.append("## Skill Update Plan")
        body_lines.append("")
        body_lines.append(skill_update_plan[:3000])
        if len(skill_update_plan) > 3000:
            body_lines.append("\n_... (truncated — see reports/skill_update_plan.md for full plan)_")
        body_lines.append("")

    if audit_report:
        body_lines.append("<details><summary>Full Audit Report</summary>")
        body_lines.append("")
        body_lines.append(audit_report[:8000])
        if len(audit_report) > 8000:
            body_lines.append("\n_... (truncated — see reports/pr_audit_report.md for full report)_")
        body_lines.append("")
        body_lines.append("</details>")
        body_lines.append("")

    body_lines.append("_This PR was created by an AI agent based on PR audit findings._")
    return "\n".join(body_lines)


def _create_github_pr(config: dict, title: str, body: str, head_branch: str) -> dict:
    """Create a GitHub PR via gh CLI (DRY: uses run_cmd same as gh/build_pr.py)."""
    org = config.get("GH_ORG", "")
    repo = config.get("GH_REPO", "")
    result = run_cmd(
        ["gh", "pr", "create", "-R", f"{org}/{repo}",
         "--title", title, "--body", body, "--head", head_branch],
        check=False,
    )
    if result.returncode != 0:
        print(f"[create-skill-pr] gh pr create failed: {result.stderr.strip()[:300]}", file=sys.stderr, flush=True)
        sys.exit(1)
    m = re.search(r'https://github\.com/[^\s]+/pull/(\d+)', result.stdout)
    if m:
        return {"url": m.group(0), "number": int(m.group(1))}
    return {"url": result.stdout.strip(), "number": 0}


def _create_bitbucket_pr(config: dict, title: str, body: str, head_branch: str, base_branch: str) -> dict:
    """Create a Bitbucket PR (DRY: delegates to bitbucket_api.create_pr same as jira/build_pr.py)."""
    workspace = config.get("BITBUCKET_WORKSPACE", "")
    repo = config.get("BITBUCKET_REPO", "")
    pr_data = bitbucket_api.create_pr(config, workspace, repo, title, body, head_branch, base_branch)
    if pr_data is None:
        print("[create-skill-pr] Bitbucket PR creation failed", file=sys.stderr, flush=True)
        sys.exit(1)
    return {
        "url": pr_data.get("links", {}).get("html", {}).get("href", ""),
        "number": pr_data.get("id", 0),
    }


def _write_empty_pr_json(config: dict) -> None:
    """Write an empty pr.json to signal no PR was created (at /workspace/pr-audit/pr.json)."""
    artifacts_write_json(config, _ISSUE_ID, "pr.json", {"url": "", "number": 0, "branch": "", "repo": "", "tracker": ""})


def _read_text_safe(path: Path) -> str:
    """Read a text file, returning empty string if missing or unreadable."""
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return ""


def _write_ygs_recommendations(reports_dir: Path, recs: list[dict]) -> None:
    """Write ygs_recommendations.md summarizing upstream skill improvement suggestions."""
    lines = ["# YGS Skill Recommendations", "", "Suggestions for upstream you-got-skills improvements:", ""]
    for rec in recs:
        skill = rec.get("skill", "unknown")
        recommendation = rec.get("recommendation", "")
        lines.append(f"## {skill}")
        lines.append(f"{recommendation}")
        lines.append("")
    (reports_dir / "ygs_recommendations.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[create-skill-pr] wrote ygs_recommendations.md ({len(recs)} recommendations)", flush=True)


if __name__ == "__main__":
    main()
