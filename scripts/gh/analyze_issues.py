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

import re
import sys

import click

from scripts.common.config import load_config
from scripts.common.gh_api import extract_github_numbers, resolve_github_issues
from scripts.common.issue_analysis import (
    emit_git_context_markers,
    resolve_skill_for_analyze,
    run_analysis,
    run_skill_analysis,
    try_git_archaeology,
    write_analysis_output,
)
from scripts.common.issue_fetcher import fetch_gh_issue_full
from scripts.gh.query_issues import _search_issues
from scripts.common.claude_runner import ensure_ygs_skills
from scripts.common.slack_format import format_for_slack
from scripts.standup.slack_client import post_report

_TRACKER = "github"



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
        body = (issue.get("body") or "").strip()
        state = issue.get("state", "")

        lines.append(f"### #{number}: {title}{label_str}")
        lines.append(f"- URL: {url}")
        lines.append(f"- Assignee: {assignee} | State: {state}")
        if body:
            lines.append(f"- Description:\n{body}")

        # First 3 comments (truncated to 500 chars each)
        comments = issue.get("comments") or []
        for i, comment in enumerate(comments[:3]):
            author = (comment.get("author") or {}).get("login", "?")
            body_short = (comment.get("body") or "").strip()[:500]
            if body_short:
                lines.append(f"- Comment by {author}: {body_short}")

        # Linked PRs
        linked_prs = issue.get("linked_prs") or []
        if linked_prs:
            pr_summaries = []
            for pr in linked_prs:
                pr_num = pr.get("number", "?")
                pr_title = pr.get("title", "")
                pr_url = pr.get("url", "")
                pr_state = pr.get("state", "")
                pr_merged = f" merged={pr['mergedAt'][:10]}" if pr.get("mergedAt") else ""
                pr_summaries.append(f"#{pr_num} {pr_title} ({pr_state}{pr_merged}) {pr_url}")
            lines.append(f"- Linked PRs: {'; '.join(pr_summaries)}")

        lines.append("")
    return "\n".join(lines)


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
        post_report(config, msg, msg, title="No issues found", filename="analysis_empty.html",
                    task_type="query")
        sys.exit(2)

    print(f"[gh-analyze] analyzing {len(raw_issues)} issue(s) — enriching with body/comments/PRs ...",
          flush=True)

    # Enrich each issue with full body, comments, and linked PRs
    enriched: list[dict] = []
    for raw in raw_issues:
        num = str(raw.get("number", ""))
        full = fetch_gh_issue_full(config, num) or raw
        enriched.append(full)

    total_pr_count = sum(len(i.get("linked_prs") or []) for i in enriched)
    print(f"[gh-analyze] enriched: {total_pr_count} linked PRs found", flush=True)

    issues_text = _format_for_analysis(enriched)
    ids = [f"#{i.get('number', '?')}" for i in enriched]

    ensure_ygs_skills()

    skill_result = resolve_skill_for_analyze(user_prompt, query, issues_text, config,
                                              log_prefix="[gh-analyze]")

    git_tracker = _TRACKER
    git_context, git_repo_path = try_git_archaeology(config, ids, tracker=git_tracker)

    try:
        if skill_result:
            skill_name, skill_path = skill_result
            print(f"[gh-analyze] using skill '{skill_name}' for analysis", flush=True)
            analysis = run_skill_analysis(config, issues_text, skill_name, skill_path,
                                          git_context=git_context,
                                          git_repo_path=git_repo_path)
        else:
            analysis = run_analysis(config, issues_text, git_context=git_context,
                                    git_repo_path=git_repo_path)
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"::add-task-context GIT_ARCHAEOLOGY::{'yes' if git_context else 'no'}", flush=True)

    git_header_line = emit_git_context_markers(config, git_context, git_repo_path, git_tracker)

    ids_str = ", ".join(ids)
    # md_text uses Markdown heading (for HTML rendering); slack_text uses mrkdwn bold
    md_text = f"# GitHub analysis of {len(enriched)} issue(s): {ids_str}\n{git_header_line}\n{analysis}"
    slack_header = f"*GitHub analysis of {len(enriched)} issue(s): {ids_str}*\n{git_header_line}\n"
    slack_text = format_for_slack(slack_header + analysis)

    print(slack_text, flush=True)
    write_analysis_output(config, ids, analysis, write_html=not bool(skill_result))
    title = f"Analysis: {ids_str}"
    filename = re.sub(r"[^a-zA-Z0-9_\-.]", "_", f"analysis_{'_'.join(ids)}.html")
    post_report(config, slack_text, md_text, title=title, filename=filename, task_type="query")

    print(f"::add-task-context SELECTED_TRACKER::github", flush=True)
    print(f"::add-task-context SELECTED_MODEL::{config.get('AI_MODEL', '')}", flush=True)
    print(f"::add-task-context ISSUE_COUNT::{len(enriched)}", flush=True)
    print(f"::add-task-context PR_LINKS_COUNT::{total_pr_count}", flush=True)
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
