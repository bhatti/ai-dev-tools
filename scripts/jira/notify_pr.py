"""Transition Jira label and post Slack notification after PR creation.

Phase 2 of create-pr: reads pr.json, updates Jira label, sends Slack message.

Usage:
    python -m scripts.jira.notify_pr --issue-id PROJ-42

Reads:  /workspace/{issue_id}/pr.json
        /workspace/{issue_id}/issue.json

Exit codes: 0=success, 1=error
"""

import sys

import click

from scripts.common.artifacts import read_json, write_json
from scripts.common.config import load_config
from scripts.common.label_utils import jira_transition_label
from scripts.common.report_renderer import build_implement_slack_text
from scripts.standup.slack_client import notify


@click.command()
@click.option("--issue-id", required=True, help="Jira issue key (e.g. PROJ-42)")
def main(issue_id: str) -> None:
    config = load_config(required=[])
    print(f"[notify-pr] issue={issue_id}", flush=True)

    pr = read_json(config, issue_id, "pr.json")
    if not pr or not pr.get("url"):
        print("ERROR: pr.json not found or missing URL", file=sys.stderr)
        sys.exit(1)

    issue = read_json(config, issue_id, "issue.json") or {}
    impl_result = read_json(config, issue_id, "impl_run_result.json") or {}
    pr_url = pr["url"]

    try:
        jira_transition_label(config, issue_id, config["INPROGRESS_LABEL"], config["PR_OPEN_LABEL"])
    except Exception as e:
        print(f"WARNING: could not update Jira label: {e}", file=sys.stderr)

    impl_text = build_implement_slack_text(issue_id, impl_result)
    notify(
        config,
        f"🤖 PR created for *{issue_id}*: {issue.get('title', '')}\n{pr_url}\n\n{impl_text}",
    )
    write_json(config, issue_id, "notify_result.json", {"status": "DONE", "pr_url": pr_url})
    print(f"[notify-pr] notified: {pr_url}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
