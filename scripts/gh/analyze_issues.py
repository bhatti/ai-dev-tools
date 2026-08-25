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
from scripts.common.issue_analysis import run_analysis, write_analysis_output
from scripts.common.shell import run_cmd as _run
from scripts.gh.query_issues import _search_issues
from scripts.standup.slack_client import build_mrkdwn_blocks, notify

_GH_ISSUE_NUM_RE = re.compile(r"#?(\d+)")
_GH_ISSUE_URL_RE = re.compile(r"github\.com/[^/]+/[^/]+/issues/(\d+)", re.IGNORECASE)


def _extract_numbers(issues_arg: str) -> list[str]:
    """Extract GitHub issue numbers from a comma-separated list of numbers or URLs."""
    numbers = []
    for part in issues_arg.split(","):
        part = part.strip()
        m = _GH_ISSUE_URL_RE.search(part)
        if m:
            numbers.append(m.group(1))
        else:
            m2 = _GH_ISSUE_NUM_RE.match(part)
            if m2:
                numbers.append(m2.group(1))
    return numbers


def _fetch_issues_by_numbers(config: dict, numbers: list[str]) -> list[dict]:
    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    issues = []
    for num in numbers:
        result = _run([
            "gh", "issue", "view", num,
            "--repo", f"{org}/{repo}",
            "--json", "number,title,url,labels,assignees,state,body",
        ])
        if result.stdout.strip():
            import json as _json
            try:
                issues.append(_json.loads(result.stdout))
            except Exception:
                print(f"[gh-analyze] warning: could not parse issue #{num}", flush=True)
        else:
            print(f"[gh-analyze] warning: issue #{num} not found", flush=True)
    return issues


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

    raw_issues: list[dict] = []
    if issues:
        numbers = _extract_numbers(issues)
        if not numbers:
            print(f"ERROR: no valid GitHub issue numbers found in: {issues}", file=sys.stderr)
            sys.exit(1)
        raw_issues = _fetch_issues_by_numbers(config, numbers)
    else:
        raw_issues = _search_issues(config, query or "", label, max_results)

    if not raw_issues:
        msg = "No GitHub issues found to analyze."
        print(msg)
        write_analysis_output(config, [], msg)
        notify(config, msg, blocks=build_mrkdwn_blocks(msg))
        sys.exit(2)

    print(f"[gh-analyze] analyzing {len(raw_issues)} issue(s) ...", flush=True)
    issues_text = _format_for_analysis(raw_issues)

    try:
        analysis = run_analysis(config, issues_text)
    except RuntimeError as e:
        print(f"ERROR: claude failed: {e}", file=sys.stderr)
        sys.exit(1)

    ids = [f"#{i.get('number', '?')}" for i in raw_issues]
    ids_str = ", ".join(ids)
    header = f"*GitHub analysis of {len(raw_issues)} issue(s): {ids_str}*\n\n"
    full_text = header + analysis

    print(full_text, flush=True)
    write_analysis_output(config, ids, analysis)
    notify(config, full_text, blocks=build_mrkdwn_blocks(full_text))
    print(f"::add-task-context SELECTED_TRACKER::github")
    print(f"::add-task-context SELECTED_MODEL::{config.get('AI_MODEL', '')}")
    print(f"::add-task-context ISSUE_COUNT::{len(raw_issues)}")
    sys.exit(0)


if __name__ == "__main__":
    main()
