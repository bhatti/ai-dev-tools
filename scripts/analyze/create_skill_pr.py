"""Create a PR proposing skill/doc improvements based on PR audit findings.

Reads reports/skill_improvements.json written by run_pr_audit.py and creates
a branch with the proposed changes, then opens a PR.

Usage:
    python -m scripts.analyze.create_skill_pr

Reads:
    /workspace/reports/skill_improvements.json
    /workspace/branch.txt

Writes:
    /workspace/pr.json
    /workspace/reports/ygs_recommendations.md
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.git_utils import clone_repo, configure_git, detect_repo_url
from scripts.standup.slack_client import post_message


def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)
    reports_dir = workspace_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    # Read skill_improvements.json
    improvements_path = reports_dir / "skill_improvements.json"
    if not improvements_path.exists():
        print("[create-skill-pr] No skill_improvements.json found", flush=True)
        _write_empty_pr_json(workspace_dir)
        print("::add-task-context SKILL_PR_CREATED::no", flush=True)
        return

    try:
        improvements = json.loads(improvements_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"[create-skill-pr] Could not parse skill_improvements.json: {e}", flush=True)
        _write_empty_pr_json(workspace_dir)
        print("::add-task-context SKILL_PR_CREATED::no", flush=True)
        return

    repo_changes = improvements.get("repo_skill_changes", [])
    new_docs = improvements.get("new_docs", [])
    ygs_recs = improvements.get("ygs_recommendations", [])

    if not repo_changes and not new_docs:
        print("[create-skill-pr] No repo changes to propose", flush=True)
        _write_empty_pr_json(workspace_dir)
        print("::add-task-context SKILL_PR_CREATED::no", flush=True)
        if ygs_recs:
            _write_ygs_recommendations(reports_dir, ygs_recs)
        return

    # Clone the repo (each task runs in a separate pod — workspace is not shared)
    tracker = (config.get("DEFAULT_TRACKER") or "").lower().strip()
    codebase_dir = Path(config.get("CODEBASE_DIR", str(workspace_dir / "repo")))
    if not (codebase_dir / ".git").exists():
        print("[create-skill-pr] Cloning repo (separate pod, workspace not persisted)...", flush=True)
        ok = _clone_repo(config, codebase_dir, tracker)
        if not ok:
            print("[create-skill-pr] Clone failed — cannot create PR", file=sys.stderr, flush=True)
            _write_empty_pr_json(workspace_dir)
            print("::add-task-context SKILL_PR_CREATED::no", flush=True)
            if ygs_recs:
                _write_ygs_recommendations(reports_dir, ygs_recs)
            return

    # Read base branch
    branch_file = workspace_dir / "branch.txt"
    base_branch = branch_file.read_text().strip() if branch_file.exists() else config.get("GH_REPO_BRANCH", config.get("BB_REPO_BRANCH", "main"))

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
                existing = target.read_text(encoding="utf-8") if target.exists() else ""
                target.write_text(existing + "\n" + content, encoding="utf-8")
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
        _write_empty_pr_json(workspace_dir)
        print("::add-task-context SKILL_PR_CREATED::no", flush=True)
        if ygs_recs:
            _write_ygs_recommendations(reports_dir, ygs_recs)
        return

    # Commit + push
    _run_git(codebase_dir, ["add", "-A"])
    commit_msg = (
        f"pr-audit: improve skills based on {len(changes_made)} changes\n\n"
        "Co-Authored-By: Claude <noreply@anthropic.com>"
    )
    _run_git(codebase_dir, ["commit", "-m", commit_msg])
    push_result = _run_git(codebase_dir, ["push", "-u", "origin", pr_branch])
    if push_result.returncode != 0:
        print("[create-skill-pr] Push failed — cannot create PR", file=sys.stderr, flush=True)
        _write_empty_pr_json(workspace_dir)
        print("::add-task-context SKILL_PR_CREATED::no", flush=True)
        if ygs_recs:
            _write_ygs_recommendations(reports_dir, ygs_recs)
        return

    # Create PR
    pr_info = _create_pr(config, codebase_dir, pr_branch, base_branch, repo_changes, new_docs, tracker)

    # Determine org/repo
    org = config.get("GH_ORG", config.get("BITBUCKET_WORKSPACE", ""))
    repo = config.get("GH_REPO", config.get("BITBUCKET_REPO", ""))

    # Write pr.json
    pr_url = pr_info.get("url", "")
    pr_num = pr_info.get("number", 0)
    pr_json = {
        "url": pr_url,
        "number": pr_num,
        "branch": pr_branch,
        "repo": f"{org}/{repo}" if org and repo else "",
        "tracker": "bitbucket" if tracker in ("jira", "bitbucket", "jira/bitbucket") else "github",
    }
    (workspace_dir / "pr.json").write_text(json.dumps(pr_json, indent=2), encoding="utf-8")

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
    """Clone the repo into dest. Returns True on success."""
    dest.parent.mkdir(parents=True, exist_ok=True)

    org = config.get("GH_ORG", "")
    repo = config.get("GH_REPO", "")
    bb_ws = config.get("BITBUCKET_WORKSPACE", "")
    bb_repo = config.get("BITBUCKET_REPO", "")
    token = config.get("GH_TOKEN", "")
    ssh_key = config.get("SSH_PRIVATE_KEY", "")
    use_ssh = not token or config.get("USE_SSH", "0") == "1"

    if tracker in ("jira", "bitbucket", "jira/bitbucket"):
        if not bb_ws or not bb_repo:
            print("[create-skill-pr] BITBUCKET_WORKSPACE/BITBUCKET_REPO not set", file=sys.stderr, flush=True)
            return False
        bb_user = config.get("BITBUCKET_USERNAME", "")
        bb_token = config.get("BITBUCKET_TOKEN", config.get("BITBUCKET_APP_PASSWORD", ""))
        if bb_user and bb_token:
            clone_url = f"https://{bb_user}:{bb_token}@bitbucket.org/{bb_ws}/{bb_repo}.git"
        else:
            clone_url = f"git@bitbucket.org:{bb_ws}/{bb_repo}.git"
        print(f"[create-skill-pr] cloning {bb_ws}/{bb_repo} (bitbucket)", flush=True)
    else:
        if not org or not repo:
            print("[create-skill-pr] GH_ORG/GH_REPO not set", file=sys.stderr, flush=True)
            return False
        if token and not use_ssh:
            clone_url = f"https://x-access-token:{token}@github.com/{org}/{repo}.git"
            print(f"[create-skill-pr] cloning {org}/{repo} via HTTPS token", flush=True)
        else:
            clone_url = detect_repo_url(org, repo, use_ssh=True)
            print(f"[create-skill-pr] cloning {org}/{repo} via SSH", flush=True)

    try:
        clone_repo(clone_url, dest, ssh_key=ssh_key if use_ssh else "")
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
    cwd: Path,
    head_branch: str,
    base_branch: str,
    repo_changes: list[dict],
    new_docs: list[dict],
    tracker: str,
) -> dict:
    """Create a PR on GitHub or Bitbucket. Returns {'url': ..., 'number': ...}."""
    title = f"[AI] PR audit: skill & doc improvements ({len(repo_changes) + len(new_docs)} changes)"

    # Build body
    body_lines = [
        "## Summary",
        "",
        "Automated improvements identified by PR audit analysis.",
        "",
    ]
    if repo_changes:
        body_lines.append("### Skill Updates")
        for ch in repo_changes[:10]:
            body_lines.append(f"- `{ch.get('file_path', '?')}`: {ch.get('description', '')}")
        if len(repo_changes) > 10:
            body_lines.append(f"- ...and {len(repo_changes) - 10} more")
        body_lines.append("")
    if new_docs:
        body_lines.append("### New Documentation")
        for d in new_docs[:10]:
            body_lines.append(f"- `{d.get('path', '?')}`: {d.get('description', '')}")
        body_lines.append("")
    body_lines.append("_This PR was created by an AI agent based on PR audit findings._")
    body = "\n".join(body_lines)

    if tracker in ("jira", "bitbucket", "jira/bitbucket"):
        return _create_bitbucket_pr(config, title, body, head_branch, base_branch)
    return _create_github_pr(config, cwd, title, body, head_branch)


def _create_github_pr(config: dict, cwd: Path, title: str, body: str, head_branch: str) -> dict:
    """Create a GitHub PR via gh CLI."""
    org = config.get("GH_ORG", "")
    repo = config.get("GH_REPO", "")
    cmd = [
        "gh", "pr", "create",
        "-R", f"{org}/{repo}",
        "--title", title,
        "--body", body,
        "--head", head_branch,
    ]
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[create-skill-pr] gh pr create failed: {e}", file=sys.stderr, flush=True)
        return {"url": "", "number": 0}

    if result.returncode != 0:
        print(f"[create-skill-pr] gh pr create error: {result.stderr.strip()[:300]}", file=sys.stderr, flush=True)
        return {"url": "", "number": 0}

    m = re.search(r'https://github\.com/[^\s]+/pull/(\d+)', result.stdout)
    if m:
        return {"url": m.group(0), "number": int(m.group(1))}
    return {"url": result.stdout.strip(), "number": 0}


def _create_bitbucket_pr(config: dict, title: str, body: str, head_branch: str, base_branch: str) -> dict:
    """Create a Bitbucket PR via REST API."""
    import requests as _requests

    workspace = config.get("BITBUCKET_WORKSPACE", "")
    repo = config.get("BITBUCKET_REPO", "")
    username = config.get("BITBUCKET_USERNAME", "")
    token = config.get("BITBUCKET_TOKEN", config.get("BITBUCKET_APP_PASSWORD", ""))

    if not all([workspace, repo, username, token]):
        print("[create-skill-pr] Bitbucket credentials incomplete", file=sys.stderr, flush=True)
        return {"url": "", "number": 0}

    url = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo}/pullrequests"
    payload = {
        "title": title,
        "description": body,
        "source": {"branch": {"name": head_branch}},
        "destination": {"branch": {"name": base_branch}},
        "close_source_branch": True,
    }
    try:
        resp = _requests.post(url, json=payload, auth=(username, token), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return {
            "url": data.get("links", {}).get("html", {}).get("href", ""),
            "number": data.get("id", 0),
        }
    except Exception as e:
        print(f"[create-skill-pr] Bitbucket PR creation failed: {e}", file=sys.stderr, flush=True)
        return {"url": "", "number": 0}


def _write_empty_pr_json(workspace: Path) -> None:
    """Write an empty pr.json to signal no PR was created."""
    pr_json = {"url": "", "number": 0, "branch": "", "repo": "", "tracker": ""}
    (workspace / "pr.json").write_text(json.dumps(pr_json, indent=2), encoding="utf-8")


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
