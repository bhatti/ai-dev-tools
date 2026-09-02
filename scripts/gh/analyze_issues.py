"""Fetch GitHub issues by number or query and analyze them with Claude.

Usage:
    # Analyze specific issues by number
    python -m scripts.gh.analyze_issues --issues "123,456"

    # Analyze issues matching a query
    python -m scripts.gh.analyze_issues --query "flaky tests" --max 10

Required env: GH_ORG, GH_REPO, GH_TOKEN
Optional env:
    ANALYSIS_PROMPT              — override the analysis prompt
    SLACK_BOT_TOKEN, SLACK_CHANNEL, SLACK_THREAD_TS (SlackThreadTs)

Exit codes: 0=success, 2=no issues found, 1=error
"""
from __future__ import annotations

import pathlib
import sys

import click

from scripts.common.config import load_config
from scripts.common.gh_api import extract_github_numbers, resolve_github_issues
from scripts.common.git_archaeology import (
    build_context as _git_build_context,
    extract_stats as _extract_stats,
    get_repo_info as _get_repo_info,
)
from scripts.common.git_utils import clone_repo, detect_repo_url
from scripts.common.issue_analysis import run_analysis, run_skill_analysis, write_analysis_output
from scripts.common.skill_resolver import find_skill, find_skill_for_query
from scripts.gh.query_issues import _search_issues
from scripts.standup.slack_client import build_mrkdwn_blocks, notify


def _format_for_analysis(issues: list[dict]) -> str:
    lines = []
    for issue in issues:
        number = issue.get("number", "?")
        title = issue.get("title", "(no title)")
        url = issue.get("url", "")
        assignees = issue.get("assignees") or []
        assignee = assignees[0].get("login", "Unassigned") if assignees else "Unassigned"
        labels = [lbl["name"] for lbl in (issue.get("labels") or [])]
        label_str = f" [{', '.join(labels)}]" if labels else ""
        body = (issue.get("body") or "").strip()[:500]
        lines.append(f"### #{number}: {title}{label_str}")
        lines.append(f"- URL: {url}")
        lines.append(f"- Assignee: {assignee}")
        if body:
            lines.append(f"- Description: {body}")
        lines.append("")
    return "\n".join(lines)


def _try_git_archaeology(config: dict, ids: list[str]) -> tuple[str | None, pathlib.Path | None]:
    """Clone the GitHub repo and run git archaeology.

    Returns (context_markdown, repo_path) or (None, None) on failure/missing config.
    """
    org = config.get("GH_ORG", "").strip()
    repo = config.get("GH_REPO", "").strip()
    if not org or not repo:
        return None, None
    token = config.get("GH_TOKEN", "").strip()
    ssh_key = config.get("SSH_PRIVATE_KEY", "").strip()
    use_ssh = not token or config.get("USE_SSH", "0") == "1"
    dest = pathlib.Path(config.get("WORKSPACE_DIR", "/tmp")) / "repo_cache"
    try:
        print(f"[gh-analyze] cloning {org}/{repo} for git archaeology ...", flush=True)
        if token and not use_ssh:
            clone_url = f"https://x-access-token:{token}@github.com/{org}/{repo}.git"
            repo_path = clone_repo(clone_url, dest, depth=50)
        else:
            clone_url = detect_repo_url(org, repo, use_ssh=True)
            repo_path = clone_repo(clone_url, dest, depth=50, ssh_key=ssh_key)
        print(f"[gh-analyze] running git archaeology on {repo_path}", flush=True)
        keys = [f"#{i.lstrip('#')}" for i in ids]
        context = _git_build_context(repo_path, keys) or None
        return context, repo_path
    except Exception as e:
        print(f"[gh-analyze] WARNING: git archaeology failed: {e} — continuing without git context", flush=True)
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
        print(f"[gh-analyze] found ygs-analyze skill directly at {direct}", flush=True)
        return ("ygs-analyze", direct)
    return find_skill_for_query(prompt or query or issues_text[:200], config)


@click.command()
@click.option("--issues", default=None,
              help="Comma-separated GitHub issue numbers or URLs to analyze")
@click.option("--query", default=None,
              help="Free-text query to find issues (same as gh-query)")
@click.option("--max", "max_results", default=10, type=int, show_default=True,
              help="Max issues to fetch when using --query")
@click.option("--label", default=None, help="Optional label filter when using --query")
@click.option("--prompt", "user_prompt", default=None,
              help="Original user query for skill resolution (e.g. the full Slack message)")
def main(issues: str | None, query: str | None, max_results: int, label: str | None,
         user_prompt: str | None) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO", "GH_TOKEN"])

    if not issues and not query:
        print("ERROR: provide --issues or --query", file=sys.stderr)
        sys.exit(1)

    if issues and not extract_github_numbers(issues):
        print(f"ERROR: no valid GitHub issue numbers found in: {issues}", file=sys.stderr)
        sys.exit(1)

    raw_issues = resolve_github_issues(
        config,
        query=query,
        issues_arg=issues,
        label=label,
        max_results=max_results,
        search_fn=_search_issues,
    )

    if not raw_issues:
        msg = "No GitHub issues found to analyze."
        print(msg)
        write_analysis_output(config, [], msg)
        notify(config, msg, blocks=build_mrkdwn_blocks(msg))
        sys.exit(2)

    print(f"[gh-analyze] analyzing {len(raw_issues)} issue(s) ...", flush=True)
    issues_text = _format_for_analysis(raw_issues)

    ids = [f"#{i.get('number', '?')}" for i in raw_issues]
    skill_result = _resolve_skill_for_analyze(user_prompt, query, issues_text, config)
    git_context: str | None = None
    git_repo_path: pathlib.Path | None = None

    try:
        if skill_result:
            skill_name, skill_path = skill_result
            print(f"[gh-analyze] using skill '{skill_name}' for analysis", flush=True)
            analysis = run_skill_analysis(config, issues_text, skill_name, skill_path)
        else:
            git_context, git_repo_path = _try_git_archaeology(config, ids)
            analysis = run_analysis(config, issues_text, git_context=git_context)
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"::add-task-context GIT_ARCHAEOLOGY::{'yes' if git_context else 'no'}", flush=True)
    ids_str = ", ".join(ids)

    git_header_line = ""
    if git_context:
        stats = _extract_stats(git_context)
        org = config.get("GH_ORG", "")
        repo = config.get("GH_REPO", "")
        repo_label = f"{org}/{repo}" if org and repo else repo or org
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

    header = f"*GitHub analysis of {len(raw_issues)} issue(s): {ids_str}*\n{git_header_line}\n"
    full_text = header + analysis

    print(full_text, flush=True)
    write_analysis_output(config, ids, analysis)
    notify(config, full_text, blocks=build_mrkdwn_blocks(full_text))
    # Emit context markers LAST so they can't be overwritten by analysis text
    # that may contain ::add-task-context lines from Claude Code's output.
    print(f"::add-task-context SELECTED_TRACKER::github", flush=True)
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
