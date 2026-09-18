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
    BITBUCKET_WORKSPACE, BITBUCKET_REPO, BITBUCKET_TOKEN, SSH_PRIVATE_KEY — for git clone
    GH_ORG, GH_REPO, GH_TOKEN    — for GitHub clone when issue links a GH repo
    SLACK_BOT_TOKEN, SLACK_CHANNEL, SLACK_THREAD_TS (SlackThreadTs)

Exit codes: 0=success, 2=no issues found, 1=error
"""
from __future__ import annotations

import re
import sys

import click

from scripts.common.config import load_config
from scripts.common.issue_analysis import (
    emit_git_context_markers,
    resolve_skill_for_analyze,
    run_analysis,
    run_skill_analysis,
    try_git_archaeology,
    write_analysis_output,
)
from scripts.common.issue_fetcher import (
    fetch_jira_attachment_text,
    fetch_jira_issue_full,
    get_jira_linked_prs,
)
from scripts.common.jira_api import extract_adf_text, extract_jira_keys, resolve_jira_issues
from scripts.jira.query_issues import _build_jql
from scripts.common.claude_runner import ensure_ygs_skills
from scripts.common.slack_format import format_for_slack
from scripts.standup.slack_client import post_report

_TRACKER = "jira"


def _patch_config_from_issue_body(issue: dict | None, config: dict) -> None:
    """Try to detect a BB or GH repo URL in the issue body and inject into config.

    Only runs when BITBUCKET_WORKSPACE/GH_ORG are not already set, so an
    explicitly configured repo always wins over an auto-detected one.
    """
    from scripts.common.issue_fetcher import detect_repo_from_issue
    detected = detect_repo_from_issue(issue, config)
    if not detected:
        return
    tracker, org, repo = detected["tracker"], detected["org"], detected["repo"]
    if tracker == "bitbucket":
        config.setdefault("BITBUCKET_WORKSPACE", org)
        config.setdefault("BITBUCKET_REPO", repo)
        print(f"[analyze] auto-detected BB repo from issue body: {org}/{repo}", flush=True)
    elif tracker == "github":
        config.setdefault("GH_ORG", org)
        config.setdefault("GH_REPO", repo)
        print(f"[analyze] auto-detected GH repo from issue body: {org}/{repo}", flush=True)


def _format_for_analysis(
    issues: list[dict],
    base_url: str,
    linked_prs_map: dict,
    attachment_texts: dict,
    config: dict,
) -> str:
    lines = []
    for issue in issues:
        key = issue.get("key", "?")
        fields = issue.get("fields", {})
        summary = fields.get("summary", "(no title)")
        status = (fields.get("status") or {}).get("name", "?")
        priority = (fields.get("priority") or {}).get("name", "None")
        assignee = (fields.get("assignee") or {}).get("displayName") or "Unassigned"
        description = extract_adf_text(fields.get("description"))
        url = f"{base_url.rstrip('/')}/browse/{key}"

        lines.append(f"### {key}: {summary}")
        lines.append(f"- URL: {url}")
        lines.append(f"- Status: {status} | Priority: {priority} | Assignee: {assignee}")
        if description and description.strip():
            lines.append(f"- Description: {description.strip()}")

        # Linked issues
        issue_links = fields.get("issuelinks") or []
        if issue_links:
            link_summaries = []
            for link in issue_links:
                rel_type = (link.get("type") or {}).get("name", "Related")
                for direction in ("inwardIssue", "outwardIssue"):
                    linked = link.get(direction)
                    if linked:
                        lkey = linked.get("key", "?")
                        lsummary = (linked.get("fields") or {}).get("summary", "")
                        link_summaries.append(f"{lkey} ({rel_type}): {lsummary}")
            if link_summaries:
                lines.append(f"- Linked issues: {'; '.join(link_summaries)}")

        # Text attachments
        attach_text = attachment_texts.get(key, "")
        if attach_text:
            lines.append(f"- Attachment content:\n{attach_text.strip()}")

        # Linked PRs
        prs = linked_prs_map.get(key, [])
        if prs:
            pr_summaries = []
            for pr in prs:
                pr_id = pr.get("id", pr.get("number", "?"))
                pr_name = pr.get("name", pr.get("title", ""))
                pr_url = pr.get("url", "")
                pr_status = pr.get("status", pr.get("state", ""))
                pr_summaries.append(f"#{pr_id} {pr_name} ({pr_status}) {pr_url}")
            lines.append(f"- Linked PRs: {'; '.join(pr_summaries)}")

        lines.append("")
    return "\n".join(lines)


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
        post_report(config, msg, msg, title="No issues found", filename="analysis_empty.html",
                    task_type="run")
        sys.exit(2)

    print(f"[analyze] analyzing {len(raw_issues)} issue(s) — enriching with links/attachments ...",
          flush=True)

    # Enrich each issue with full fields (attachments + issuelinks)
    enriched: list[dict] = []
    for raw in raw_issues:
        full = fetch_jira_issue_full(config, raw["key"]) or raw
        enriched.append(full)

    # Collect linked PRs for each issue
    linked_prs_map: dict[str, list] = {}
    total_pr_count = 0
    for issue in enriched:
        key = issue.get("key", "")
        issue_id = issue.get("id", "")
        prs = get_jira_linked_prs(config, key, issue_id)
        linked_prs_map[key] = prs
        total_pr_count += len(prs)

    # Collect text attachment content
    attachment_texts: dict[str, str] = {}
    total_attach_count = 0
    for issue in enriched:
        key = issue.get("key", "")
        fields = issue.get("fields", {})
        attachments = fields.get("attachment") or []
        parts = []
        for att in attachments:
            text = fetch_jira_attachment_text(config, att)
            if text:
                fname = att.get("filename", "attachment")
                parts.append(f"[{fname}]\n{text}")
                total_attach_count += 1
        if parts:
            attachment_texts[key] = "\n\n".join(parts)

    total_link_count = sum(
        len((i.get("fields") or {}).get("issuelinks") or []) for i in enriched
    )
    print(f"[analyze] enriched: {total_link_count} issue links, "
          f"{total_pr_count} PRs, {total_attach_count} attachments", flush=True)

    # Auto-detect repo from issue body if no repo configured in env
    if not (config.get("BITBUCKET_WORKSPACE") or config.get("GH_ORG")):
        _patch_config_from_issue_body(enriched[0] if enriched else None, config)

    issues_text = _format_for_analysis(enriched, base_url, linked_prs_map, attachment_texts,
                                        config)

    # Ensure YGS skills are installed before skill resolution so ygs-analyze is discoverable.
    ensure_ygs_skills()

    skill_result = resolve_skill_for_analyze(user_prompt, query, issues_text, config,
                                              log_prefix="[analyze]")
    keys_for_archaeology = [i.get("key") for i in enriched if i.get("key")]

    # Determine tracker for git archaeology (may have been updated by _patch_config_from_issue_body)
    git_tracker = _TRACKER
    if config.get("GH_ORG") and not config.get("BITBUCKET_WORKSPACE"):
        git_tracker = "github"

    git_context, git_repo_path = try_git_archaeology(config, keys_for_archaeology,
                                                       tracker=git_tracker)

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

    git_header_line = emit_git_context_markers(config, git_context, git_repo_path, git_tracker)

    keys_list = [i.get("key", "?") for i in enriched]
    keys_str = ", ".join(keys_list)
    header = f"*Analysis of {len(enriched)} issue(s): {keys_str}*\n{git_header_line}\n"
    md_analysis = header + analysis
    slack_text = format_for_slack(md_analysis)

    print(slack_text, flush=True)
    write_analysis_output(config, keys_list, analysis, write_html=not bool(skill_result))
    title = f"Analysis: {keys_str}"
    filename = re.sub(r"[^a-zA-Z0-9_\-.]", "_", f"analysis_{'_'.join(keys_list)}.html")
    post_report(config, slack_text, md_analysis, title=title, filename=filename, task_type="run")

    print(f"::add-task-context SELECTED_TRACKER::jira", flush=True)
    print(f"::add-task-context SELECTED_MODEL::{config.get('AI_MODEL', '')}", flush=True)
    print(f"::add-task-context ISSUE_COUNT::{len(enriched)}", flush=True)
    print(f"::add-task-context ISSUE_LINKS_COUNT::{total_link_count}", flush=True)
    print(f"::add-task-context PR_LINKS_COUNT::{total_pr_count}", flush=True)
    print(f"::add-task-context ATTACHMENTS_COUNT::{total_attach_count}", flush=True)
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
