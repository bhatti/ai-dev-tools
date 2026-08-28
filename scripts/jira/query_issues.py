"""Search Jira issues by free-text query and post results to Slack.

Usage:
    python -m scripts.jira.query_issues --query "flaky tests"
    python -m scripts.jira.query_issues --query "unassigned bugs" --max 20

Required env: JIRA_PROJECT, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_BASE_URL
Optional env:
    JIRA_SPACE        — scrum-team / area filter value; defaults to BITBUCKET_WORKSPACE.
                        Matched against the field named by JIRA_TEAM_FIELD (see below).
    JIRA_TEAM_FIELD   — Jira custom field name for the team/area dimension.
                        Defaults to "EngScrumTeam". Set to "" to disable the filter.
    SLACK_BOT_TOKEN, SLACK_CHANNEL, SLACK_THREAD_TS (SlackThreadTs)

The query is used to filter Jira issue summaries (summary ~ "<query>").
The result is formatted as a compact list and posted to Slack.
Exit codes: 0=success, 2=no results, 1=error
"""

import json
import sys
from pathlib import Path

import click
import requests

from scripts.common.config import load_config, get_workspace_dir
from scripts.common.jira_api import _auth_headers, _base, search_issues
from scripts.common.report_renderer import render_simple_html
from scripts.standup.slack_client import build_issue_blocks, notify


def _resolve_team_field_id(config: dict, field_name: str) -> str | None:
    """Return the Jira field ID for a given field name, or None if not found."""
    url = f"{_base(config)}/rest/api/3/field"
    try:
        resp = requests.get(url, headers=_auth_headers(config), timeout=15)
        if not resp.ok:
            return None
        for f in resp.json():
            if f.get("name", "").lower() == field_name.lower():
                return f.get("id")
    except Exception:
        pass
    return None


def _build_jql(config: dict, query: str, issue_type: str | None = None) -> str:
    """Build JQL for a free-text query against project + optional team field."""
    project = config["JIRA_PROJECT"]
    # JIRA_SPACE: team/area value. Defaults to BITBUCKET_WORKSPACE if not set.
    space = config.get("JIRA_SPACE") or config.get("BITBUCKET_WORKSPACE") or ""
    # JIRA_TEAM_FIELD: Jira field name for the team dimension. Set to "" to disable.
    team_field_name = config.get("JIRA_TEAM_FIELD", "EngScrumTeam")

    parts = [f'project = "{project}"']
    if issue_type:
        parts.append(f'issuetype = "{issue_type}"')
    if space and team_field_name:
        field_id = _resolve_team_field_id(config, team_field_name)
        if field_id:
            parts.append(f'{field_id} = "{space}"')
        else:
            print(f"[jira-query] warning: field '{team_field_name}' not found — skipping team filter", flush=True)
    parts.append('status not in ("Done", "Close", "Closed")')
    if query:
        safe = query.replace("\\", "\\\\").replace('"', '\\"')
        parts.append(f'summary ~ "{safe}"')
    parts.append("ORDER BY priority DESC, created DESC")
    return " AND ".join(parts[:-1]) + " " + parts[-1]


def _extract_plain_text(body) -> str:
    """Extract plain text from Jira ADF or string description."""
    if not body:
        return ""
    if isinstance(body, str):
        return body.strip()
    if isinstance(body, dict):
        # Atlassian Document Format — recursively collect text nodes
        parts = []
        def _collect(node, depth=0):
            if depth > 8:
                return
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content", []):
                _collect(child, depth + 1)
        _collect(body)
        return " ".join(p for p in parts if p).strip()
    return ""


def _format_issue(issue: dict, base_url: str) -> str:
    key = issue.get("key", "?")
    fields = issue.get("fields", {})
    summary = fields.get("summary", "(no title)")
    status = (fields.get("status") or {}).get("name", "?")
    issuetype = (fields.get("issuetype") or {}).get("name", "")
    assignee_obj = fields.get("assignee") or {}
    assignee = assignee_obj.get("displayName") or "Unassigned"
    priority_obj = fields.get("priority") or {}
    priority = priority_obj.get("name") or "None"
    created = (fields.get("created") or "")[:10]  # YYYY-MM-DD
    desc_raw = _extract_plain_text(fields.get("description"))
    desc = (desc_raw[:120] + "…") if len(desc_raw) > 120 else desc_raw
    url = f"{base_url.rstrip('/')}/browse/{key}"

    type_tag = f"[{issuetype}] " if issuetype else ""
    meta = f"_{assignee}_ · {status} · priority: {priority}"
    if created:
        meta += f" · {created}"
    line = f"• <{url}|{key}> {type_tag}{summary} — {meta}"
    if desc:
        line += f"\n  _{desc}_"
    return line


def _write_query_output(config: dict, query: str, issues: list, base_url: str) -> None:
    workspace = get_workspace_dir(config)
    reports = workspace / "reports"
    reports.mkdir(parents=True, exist_ok=True)

    result = {
        "query": query,
        "count": len(issues),
        "source": "jira",
        "issues": [
            {
                "key": i.get("key", ""),
                "summary": (i.get("fields") or {}).get("summary", ""),
                "status": ((i.get("fields") or {}).get("status") or {}).get("name", ""),
                "assignee": ((i.get("fields") or {}).get("assignee") or {}).get("displayName") or "Unassigned",
                "priority": ((i.get("fields") or {}).get("priority") or {}).get("name") or "None",
                "url": f"{base_url.rstrip('/')}/browse/{i.get('key', '')}",
            }
            for i in issues
        ],
    }
    (reports / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    lines = [f"# Jira Issues: \"{query}\" ({len(issues)} found)\n"]
    for issue in result["issues"]:
        lines.append(
            f"- [{issue['key']}]({issue['url']}) {issue['summary']}"
            f" — _{issue['assignee']}_ · {issue['status']} · {issue['priority']}"
        )
    md_text = "\n".join(lines)
    (reports / "report.md").write_text(md_text, encoding="utf-8")

    title = f"Jira Issues: \"{query}\" ({len(issues)} found)"
    (reports / "report.html").write_text(render_simple_html(title, md_text), encoding="utf-8")
    print(f"[jira-query] wrote reports/result.json, reports/report.md, reports/report.html", flush=True)


@click.command()
@click.option("--query", required=True, help="Free-text search term (used in summary ~ filter)")
@click.option("--issue-type", default=None, help="Optional issuetype filter (e.g. Bug, Story)")
@click.option("--max", "max_results", default=20, type=int, show_default=True, help="Max results")
def main(query: str, issue_type: str | None, max_results: int) -> None:
    config = load_config(required=["JIRA_PROJECT", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_BASE_URL"])
    base_url = config["JIRA_BASE_URL"].rstrip("/")

    jql = _build_jql(config, query, issue_type)
    print(f"[jira-query] JQL: {jql}", flush=True)

    issues = search_issues(config, jql, max_results=max_results)
    if not issues:
        msg = f"No open Jira issues matching *{query}*."
        print(msg)
        _write_query_output(config, query, [], base_url)
        notify(config, msg)
        sys.exit(2)

    # Build plain-text fallback (stdout + notification fallback)
    lines = [f"Jira issues matching \"{query}\" ({len(issues)} found):"]
    for issue in issues:
        lines.append(_format_issue(issue, base_url))
    text = "\n".join(lines)
    print(text, flush=True)

    # Build Block Kit blocks for structured Slack output
    title = f"Jira issues matching \"{query}\" ({len(issues)} found)"
    blocks = build_issue_blocks(title, issues, base_url)
    _write_query_output(config, query, issues, base_url)
    notify(config, text, blocks=blocks)
    print(f"::add-task-context SELECTED_TRACKER::jira", flush=True)
    print(f"::add-task-context ISSUE_COUNT::{len(issues)}", flush=True)
    print(f"::add-task-context QUERY::{query}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
