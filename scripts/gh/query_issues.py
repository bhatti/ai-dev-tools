"""Search GitHub issues by free-text query and post results to Slack.

Usage:
    python -m scripts.gh.query_issues --query "flaky tests"
    python -m scripts.gh.query_issues --query "memory leak" --max 20 --label bug

Required env: GH_ORG, GH_REPO, GH_TOKEN
Optional env: SLACK_BOT_TOKEN, SLACK_CHANNEL, SLACK_THREAD_TS (SlackThreadTs)

Exit codes: 0=success, 2=no results, 1=error
"""

import json
import sys

import click

from scripts.common.config import load_config, get_workspace_dir
from scripts.common.report_renderer import render_simple_html
from scripts.common.shell import run_cmd as _run
from scripts.standup.slack_client import build_gh_issue_blocks, notify


def _search_issues(config: dict, query: str, label: str | None, max_results: int) -> list[dict]:
    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    cmd = [
        "gh", "search", "issues", query,
        "--repo", f"{org}/{repo}",
        "--state", "open",
        "--limit", str(max_results),
        "--json", "number,title,url,labels,assignees,state,createdAt",
    ]
    if label:
        cmd += ["--label", label]
    result = _run(cmd)
    if not result.stdout.strip():
        return []
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse gh output: {e}", file=sys.stderr)
        return []


def _format_issue(issue: dict) -> str:
    number = issue.get("number", "?")
    title = issue.get("title", "(no title)")
    url = issue.get("url", "")
    assignees = issue.get("assignees") or []
    assignee = assignees[0].get("login", "Unassigned") if assignees else "Unassigned"
    labels = [l["name"] for l in (issue.get("labels") or [])]
    label_str = f" [{', '.join(labels)}]" if labels else ""
    return f"• <{url}|#{number}>{label_str} {title} — _{assignee}_"


def _write_query_output(config: dict, query: str, issues: list) -> None:
    workspace = get_workspace_dir(config)
    reports = workspace / "reports"
    reports.mkdir(parents=True, exist_ok=True)

    result = {
        "query": query,
        "count": len(issues),
        "source": "github",
        "issues": [
            {
                "number": i.get("number"),
                "title": i.get("title", ""),
                "url": i.get("url", ""),
                "assignee": (i.get("assignees") or [{}])[0].get("login", "Unassigned") if i.get("assignees") else "Unassigned",
                "labels": [l["name"] for l in (i.get("labels") or [])],
            }
            for i in issues
        ],
    }
    (reports / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")

    lines = [f"# GitHub Issues: \"{query}\" ({len(issues)} found)\n"]
    for issue in result["issues"]:
        labels = f" [{', '.join(issue['labels'])}]" if issue["labels"] else ""
        lines.append(f"- [#{issue['number']}]({issue['url']}){labels} {issue['title']} — _{issue['assignee']}_")
    md_text = "\n".join(lines)
    (reports / "report.md").write_text(md_text, encoding="utf-8")

    title = f"GitHub Issues: \"{query}\" ({len(issues)} found)"
    (reports / "report.html").write_text(render_simple_html(title, md_text), encoding="utf-8")
    print(f"[gh-query] wrote reports/result.json, reports/report.md, reports/report.html", flush=True)


@click.command()
@click.option("--query", required=True, help="Search text (title/body match)")
@click.option("--label", default=None, help="Optional label filter")
@click.option("--max", "max_results", default=20, type=int, show_default=True, help="Max results")
def main(query: str, label: str | None, max_results: int) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO", "GH_TOKEN"])

    print(f"[gh-query] searching: query={query!r} label={label}", flush=True)
    issues = _search_issues(config, query, label, max_results)

    if not issues:
        msg = f"No open GitHub issues matching *{query}*."
        print(msg)
        _write_query_output(config, query, [])
        notify(config, msg)
        sys.exit(2)

    lines = [f"*GitHub issues matching \"{query}\"* ({len(issues)} found):"]
    for issue in issues:
        lines.append(_format_issue(issue))

    text = "\n".join(lines)
    print(text, flush=True)
    _write_query_output(config, query, issues)
    title = f"GitHub issues matching \"{query}\" ({len(issues)} found)"
    blocks = build_gh_issue_blocks(title, issues)
    notify(config, text, blocks=blocks)
    print(f"::add-task-context SELECTED_TRACKER::github")
    print(f"::add-task-context ISSUE_COUNT::{len(issues)}")
    print(f"::add-task-context QUERY::{query}")
    sys.exit(0)


if __name__ == "__main__":
    main()
