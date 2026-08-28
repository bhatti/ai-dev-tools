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

import re
import sys

import click

from scripts.common.config import load_config
from scripts.common.issue_analysis import run_analysis, write_analysis_output
from scripts.common.jira_api import get_issue, search_issues
from scripts.jira.query_issues import _build_jql
from scripts.standup.slack_client import build_mrkdwn_blocks, notify

_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")
_JIRA_URL_RE = re.compile(r"https?://[^/]+/browse/([A-Z][A-Z0-9_]+-\d+)")


def _extract_keys(issues_arg: str) -> list[str]:
    """Extract Jira issue keys from a comma-separated list of keys or URLs."""
    keys = []
    for part in issues_arg.split(","):
        part = part.strip()
        m = _JIRA_URL_RE.search(part)
        if m:
            keys.append(m.group(1))
        else:
            m2 = _JIRA_KEY_RE.match(part)
            if m2:
                keys.append(m2.group(1))
    return keys


def _fetch_issues_by_keys(config: dict, keys: list[str]) -> list[dict]:
    issues = []
    for key in keys:
        raw = get_issue(config, key)
        if raw:
            issues.append(raw)
        else:
            print(f"[analyze] warning: issue {key} not found or no access", flush=True)
    return issues


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

    raw_issues: list[dict] = []
    if issues:
        keys = _extract_keys(issues)
        if not keys:
            print(f"ERROR: no valid Jira keys found in: {issues}", file=sys.stderr)
            sys.exit(1)
        raw_issues = _fetch_issues_by_keys(config, keys)
    else:
        jql = _build_jql(config, query or "", issue_type)
        print(f"[analyze] JQL: {jql}", flush=True)
        raw_issues = search_issues(config, jql, max_results=max_results)

    if not raw_issues:
        msg = "No Jira issues found to analyze."
        print(msg)
        write_analysis_output(config, [], msg)
        notify(config, msg, blocks=build_mrkdwn_blocks(msg))
        sys.exit(2)

    print(f"[analyze] analyzing {len(raw_issues)} issue(s) ...", flush=True)
    issues_text = _format_for_analysis(raw_issues, base_url)

    try:
        analysis = run_analysis(config, issues_text)
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr)
        sys.exit(1)

    keys_list = [i.get("key", "?") for i in raw_issues]
    keys_str = ", ".join(keys_list)
    header = f"*Analysis of {len(raw_issues)} issue(s): {keys_str}*\n\n"
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
