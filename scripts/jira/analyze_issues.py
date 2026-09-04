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
from scripts.common.git_archaeology import (
    build_context as _git_build_context,
    extract_stats as _extract_stats,
    get_repo_info as _get_repo_info,
)
from scripts.common.git_utils import clone_repo, detect_bitbucket_url
from scripts.common.issue_analysis import run_analysis, run_skill_analysis, write_analysis_output
from scripts.common.jira_api import extract_jira_keys, resolve_jira_issues
from scripts.common.skill_resolver import find_skill, find_skill_for_query
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


def _try_git_archaeology(config: dict, keys: list[str]) -> tuple[str | None, pathlib.Path | None]:
    """Clone the Bitbucket repo and run git archaeology.

    Returns (context_markdown, repo_path) or (None, None) on failure/missing config.
    """
    workspace = config.get("BITBUCKET_WORKSPACE", "").strip()
    repo = config.get("BITBUCKET_REPO", "").strip()
    if not workspace or not repo:
        return None, None
    ssh_key = config.get("SSH_PRIVATE_KEY", "").strip()
    http_token = config.get("BITBUCKET_TOKEN", "").strip()
    dest = pathlib.Path(config.get("WORKSPACE_DIR", "/tmp")) / "repo_cache"
    try:
        ssh_len = len(ssh_key)
        tok_len = len(http_token)
        print(f"[analyze] cloning {workspace}/{repo} for git archaeology "
              f"(ssh_key_len={ssh_len} token_len={tok_len}) ...", flush=True)
        # Prefer SSH when key is available — more reliable than HTTP token auth
        if ssh_key:
            clone_url = detect_bitbucket_url(workspace, repo, use_ssh=True)
            print(f"[analyze] using SSH clone", flush=True)
            repo_path = clone_repo(clone_url, dest, depth=50, ssh_key=ssh_key)
        elif http_token:
            http_username = config.get("BITBUCKET_USERNAME", "x-token-auth")
            clone_url = detect_bitbucket_url(workspace, repo, use_ssh=False)
            print(f"[analyze] using HTTPS clone", flush=True)
            repo_path = clone_repo(clone_url, dest, depth=50, http_token=http_token, http_username=http_username)
        else:
            print(f"[analyze] WARNING: no SSH key or HTTP token — cannot clone {workspace}/{repo}", flush=True)
            print(f"::add-task-context CLONE_METHOD::none", flush=True)
            return None, None
        clone_method = "ssh" if ssh_key else "https"
        print(f"::add-task-context CLONE_METHOD::{clone_method}", flush=True)
        print(f"[analyze] running git archaeology on {repo_path}", flush=True)
        context = _git_build_context(repo_path, keys) or None
        return context, repo_path
    except Exception as e:
        print(f"[analyze] WARNING: git archaeology failed: {e} — continuing without git context", flush=True)
        print(f"::add-task-context CLONE_METHOD::failed", flush=True)
        return None, None


def _resolve_skill_for_analyze(
    prompt: str | None, query: str | None, issues_text: str, config: dict
) -> tuple[str, pathlib.Path] | None:
    """Find the best skill for the analyze workflow.

    Tries direct ygs-analyze lookup first (since this IS the analyze script),
    then falls back to keyword-based matching with the user's prompt.
    """
    direct = find_skill("ygs-analyze", config)
    if direct:
        print(f"[analyze] found ygs-analyze skill directly at {direct}", flush=True)
        return ("ygs-analyze", direct)
    return find_skill_for_query(prompt or query or issues_text[:200], config)


@click.command()
@click.option("--issues", default=None,
              help="Comma-separated Jira issue keys or URLs to analyze")
@click.option("--query", default=None,
              help="Free-text query to find issues (uses same JQL as jira-query)")
@click.option("--max", "max_results", default=10, type=int, show_default=True,
              help="Max issues to fetch when using --query")
@click.option("--issue-type", default=None, help="issuetype filter when using --query")
@click.option("--prompt", "user_prompt", default=None,
              help="Original user query for skill resolution (e.g. the full Slack message)")
def main(issues: str | None, query: str | None, max_results: int, issue_type: str | None,
         user_prompt: str | None) -> None:
    # JIRA_PROJECT only needed for --query (JQL); when --issues provides keys directly, skip it
    required = ["JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_BASE_URL"]
    if not issues:
        required.append("JIRA_PROJECT")
    config = load_config(required=required)
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

    skill_result = _resolve_skill_for_analyze(user_prompt, query, issues_text, config)
    keys_for_archaeology = [i.get("key") for i in raw_issues if i.get("key")]

    # Always attempt git archaeology when credentials are available — provides
    # commit history context regardless of whether a skill is used.
    git_context, git_repo_path = _try_git_archaeology(config, keys_for_archaeology)

    try:
        if skill_result:
            skill_name, skill_path = skill_result
            print(f"[analyze] using skill '{skill_name}' for analysis", flush=True)
            analysis = run_skill_analysis(config, issues_text, skill_name, skill_path,
                                          git_context=git_context)
        else:
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
        if parts:
            git_header_line = f"📂 *Git context:* {', '.join(parts)}\n"

    header = f"*Analysis of {len(raw_issues)} issue(s): {keys_str}*\n{git_header_line}\n"
    full_text = header + analysis

    print(full_text, flush=True)
    write_analysis_output(config, keys_list, analysis)
    notify(config, full_text, blocks=build_mrkdwn_blocks(full_text))
    # Emit context markers LAST so they can't be overwritten by analysis text
    # that may contain ::add-task-context lines from Claude Code's output.
    print(f"::add-task-context SELECTED_TRACKER::jira", flush=True)
    print(f"::add-task-context SELECTED_MODEL::{config.get('AI_MODEL', '')}", flush=True)
    print(f"::add-task-context ISSUE_COUNT::{len(raw_issues)}", flush=True)
    if skill_result:
        print(f"::add-task-context SKILL_USED::{skill_result[0]}", flush=True)
        print(f"::add-task-context ANALYSIS_TYPE::skill", flush=True)
    elif git_context:
        print(f"::add-task-context ANALYSIS_TYPE::git-archaeology", flush=True)
    else:
        print(f"::add-task-context ANALYSIS_TYPE::basic", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
