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
from scripts.common.git_archaeology import build_context as _git_build_context, extract_stats as _extract_stats
from scripts.common.git_utils import clone_repo, detect_repo_url
from scripts.common.issue_analysis import run_analysis, write_analysis_output
from scripts.common.skill_resolver import find_skill_for_query
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


def _try_git_archaeology(config: dict, ids: list[str]) -> str | None:
    """Clone the GitHub repo and run git archaeology. Returns Markdown context or None.

    Follows the same HTTPS-first / SSH-fallback pattern as scripts/gh/clone_repo.py.
    """
    org = config.get("GH_ORG", "").strip()
    repo = config.get("GH_REPO", "").strip()
    if not org or not repo:
        return None
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
        return _git_build_context(repo_path, keys) or None
    except Exception as e:
        print(f"[gh-analyze] WARNING: git archaeology failed: {e} — continuing without git context", flush=True)
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
              help="Comma-separated GitHub issue numbers or URLs to analyze")
@click.option("--query", default=None,
              help="Free-text query to find issues (same as gh-query)")
@click.option("--max", "max_results", default=10, type=int, show_default=True,
              help="Max issues to fetch when using --query")
@click.option("--label", default=None, help="Optional label filter when using --query")
def main(issues: str | None, query: str | None, max_results: int, label: str | None) -> None:
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
    skill_result = find_skill_for_query(query or issues_text[:200], config)
    git_context: str | None = None

    try:
        if skill_result:
            skill_name, skill_path = skill_result
            print(f"[gh-analyze] using skill '{skill_name}' for analysis", flush=True)
            analysis = _run_skill_analysis(config, issues_text, skill_name, pathlib.Path(skill_path))
            print(f"::add-task-context SKILL_USED::{skill_name}", flush=True)
        else:
            git_context = _try_git_archaeology(config, ids)
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
    print(f"::add-task-context SELECTED_TRACKER::github", flush=True)
    print(f"::add-task-context SELECTED_MODEL::{config.get('AI_MODEL', '')}", flush=True)
    print(f"::add-task-context ISSUE_COUNT::{len(raw_issues)}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
