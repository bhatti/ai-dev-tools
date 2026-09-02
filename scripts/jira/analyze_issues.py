"""Fetch Jira issues by key or JQL query and analyze them with Claude.

Usage:
    # Analyze specific issues by URL or key
    python -m scripts.jira.analyze_issues --issues "PROJ-123,PROJ-124"
    python -m scripts.jira.analyze_issues --issues "https://company.atlassian.net/browse/PROJ-123"

    # Analyze a set of issues matching a query
    python -m scripts.jira.analyze_issues --query "flaky tests" --max 10

Required env: JIRA_PROJECT, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_BASE_URL
Optional env:
    JIRA_SPACE, JIRA_TEAM_FIELD  — same as query_issues (team filter)
    ANALYSIS_PROMPT              — override the analysis prompt
    SLACK_BOT_TOKEN, SLACK_CHANNEL, SLACK_THREAD_TS (SlackThreadTs)

Exit codes: 0=success, 2=no issues found, 1=error
"""

import pathlib
import re
import sys

import click

from scripts.common.config import load_config
from scripts.common.git_archaeology import build_context as _git_build_context, extract_stats as _extract_stats
from scripts.common.git_utils import clone_repo, detect_bitbucket_url
from scripts.common.issue_analysis import run_analysis, write_analysis_output
from scripts.common.jira_api import extract_jira_keys, resolve_jira_issues
from scripts.common.skill_resolver import find_skill_for_query
from scripts.jira.query_issues import _build_jql
from scripts.standup.slack_client import build_mrkdwn_blocks, notify


def _format_for_analysis(issues: list[dict], base_url: str) -> str:
    lines = []
    for issue in issues:
        key = issue.get("key", "?")
        fields = issue.get("fields", {})
        summary = fields.get("summary", "(no title)")
        status = (fields.get("status") or {}).get("name", "?")
        priority = (fields.get("priority") or {}).get("name", "None")
        assignee = (fields.get("assignee") or {}).get("displayName") or "Unassigned"
        body = fields.get("description") or ""
        if isinstance(body, dict):
            body = _extract_text_from_doc(body)
        url = f"{base_url.rstrip('/')}/browse/{key}"
        lines.append(f"### {key}: {summary}")
        lines.append(f"- URL: {url}")
        lines.append(f"- Status: {status} | Priority: {priority} | Assignee: {assignee}")
        if body and body.strip():
            lines.append(f"- Description: {body.strip()[:500]}")
        lines.append("")
    return "\n".join(lines)


def _extract_text_from_doc(doc: dict | None, depth: int = 0) -> str:
    """Recursively extract plain text from Jira's Atlassian Document Format."""
    if not doc or depth > 10:
        return ""
    if doc.get("type") == "text":
        return doc.get("text", "")
    parts = []
    for child in doc.get("content", []):
        parts.append(_extract_text_from_doc(child, depth + 1))
    return " ".join(p for p in parts if p)


def _try_git_archaeology(config: dict, keys: list[str]) -> str | None:
    """Clone the Bitbucket repo and run git archaeology. Returns Markdown context or None.

    Follows the same HTTPS-first / SSH-fallback pattern as scripts/jira/clone_repo.py.
    """
    workspace = config.get("BITBUCKET_WORKSPACE", "").strip()
    repo = config.get("BITBUCKET_REPO", "").strip()
    if not workspace or not repo:
        return None
    http_token = config.get("BITBUCKET_TOKEN", "").strip()
    ssh_key = config.get("SSH_PRIVATE_KEY", "").strip()
    dest = pathlib.Path(config.get("WORKSPACE_DIR", "/tmp")) / "repo_cache"
    try:
        print(f"[analyze] cloning {workspace}/{repo} for git archaeology ...", flush=True)
        if http_token:
            http_username = config.get("BITBUCKET_USERNAME", "x-token-auth")
            clone_url = detect_bitbucket_url(workspace, repo, use_ssh=False)
            repo_path = clone_repo(clone_url, dest, depth=50, http_token=http_token, http_username=http_username)
        else:
            clone_url = detect_bitbucket_url(workspace, repo, use_ssh=True)
            repo_path = clone_repo(clone_url, dest, depth=50, ssh_key=ssh_key)
        print(f"[analyze] running git archaeology on {repo_path}", flush=True)
        return _git_build_context(repo_path, keys) or None
    except Exception as e:
        print(f"[analyze] WARNING: git archaeology failed: {e} — continuing without git context", flush=True)
        return None


def _run_skill_analysis(config: dict, issues_text: str, skill_name: str, skill_path: pathlib.Path) -> str:
    """Invoke a skill's SKILL.md instructions for analysis via Claude."""
    from scripts.common.claude_runner import run_claude, SYSTEM_PROMPTS
    skill_md = skill_path.read_text(encoding="utf-8")
    prompt = f"{skill_md}\n\n## Issue Context to Analyze\n\n{issues_text}"
    workspace = pathlib.Path(config.get("WORKSPACE_DIR", "/tmp"))
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


@click.command()
@click.option("--issues", default=None,
              help="Comma-separated Jira issue keys or URLs to analyze")
@click.option("--query", default=None,
              help="Free-text query to find issues (uses same JQL as jira-query)")
@click.option("--max", "max_results", default=10, type=int, show_default=True,
              help="Max issues to fetch when using --query")
@click.option("--issue-type", default=None, help="issuetype filter when using --query")
def main(issues: str | None, query: str | None, max_results: int, issue_type: str | None) -> None:
    config = load_config(required=["JIRA_PROJECT", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_BASE_URL"])
    base_url = config["JIRA_BASE_URL"].rstrip("/")

    if not issues and not query:
        print("ERROR: provide --issues or --query", file=sys.stderr)
        sys.exit(1)

    if issues and not extract_jira_keys(issues):
        print(f"ERROR: no valid Jira keys found in: {issues}", file=sys.stderr)
        sys.exit(1)
    raw_issues = resolve_jira_issues(
        config,
        query=query,
        issues_arg=issues,
        issue_type=issue_type,
        max_results=max_results,
        build_jql_fn=_build_jql,
    )

    if not raw_issues:
        msg = "No Jira issues found to analyze."
        print(msg)
        write_analysis_output(config, [], msg)
        notify(config, msg, blocks=build_mrkdwn_blocks(msg))
        sys.exit(2)

    print(f"[analyze] analyzing {len(raw_issues)} issue(s) ...", flush=True)
    issues_text = _format_for_analysis(raw_issues, base_url)

    skill_result = find_skill_for_query(query or issues_text[:200], config)
    git_context: str | None = None

    try:
        if skill_result:
            skill_name, skill_path = skill_result
            print(f"[analyze] using skill '{skill_name}' for analysis", flush=True)
            analysis = _run_skill_analysis(config, issues_text, skill_name, pathlib.Path(skill_path))
            print(f"::add-task-context SKILL_USED::{skill_name}", flush=True)
        else:
            git_context = _try_git_archaeology(config, [i.get("key") for i in raw_issues if i.get("key")])
            analysis = run_analysis(config, issues_text, git_context=git_context)
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"::add-task-context GIT_ARCHAEOLOGY::{'yes' if git_context else 'no'}", flush=True)

    keys_list = [i.get("key", "?") for i in raw_issues]
    keys_str = ", ".join(keys_list)

    git_header_line = ""
    if git_context:
        stats = _extract_stats(git_context)
        ws = config.get("BITBUCKET_WORKSPACE", "")
        repo = config.get("BITBUCKET_REPO", "")
        repo_label = f"{ws}/{repo}" if ws and repo else repo or ws
        print(f"::add-task-context GIT_REPO::{repo_label}", flush=True)
        print(f"::add-task-context GIT_COMMITS_FOUND::{stats['commits_found']}", flush=True)
        print(f"::add-task-context GIT_HOT_FILES::{stats['hot_files']}", flush=True)
        parts = [f"cloned `{repo_label}`"] if repo_label else []
        if stats["commits_found"]:
            parts.append(f"{stats['commits_found']} related commits")
        if stats["top_hot_file"]:
            parts.append(f"hottest: `{stats['top_hot_file']}`")
        if parts:
            git_header_line = f"📂 *Git context:* {', '.join(parts)}\n"

    header = f"*Analysis of {len(raw_issues)} issue(s): {keys_str}*\n{git_header_line}\n"
    full_text = header + analysis

    print(full_text, flush=True)
    write_analysis_output(config, keys_list, analysis)
    notify(config, full_text, blocks=build_mrkdwn_blocks(full_text))
    print(f"::add-task-context SELECTED_TRACKER::jira", flush=True)
    print(f"::add-task-context SELECTED_MODEL::{config.get('AI_MODEL', '')}", flush=True)
    print(f"::add-task-context ISSUE_COUNT::{len(raw_issues)}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
