"""Check BitBucket PR state and run learn/notify if terminal.

Phase 1 of poll-pr: determines PR state, handles terminal states (MERGED/DECLINED).
Writes poll_state.json so downstream phases know whether to proceed.

Usage:
    python -m scripts.jira.check_pr_state --issue-id PROJ-42

Reads:  /workspace/{issue_id}/pr.json
Writes: /workspace/{issue_id}/monitor_result.json  (only if terminal)
        /workspace/{issue_id}/poll_state.json

Exit codes: 0=ok (open or terminal handled), 1=error
"""

import subprocess
import sys

import click

from scripts.common.artifacts import read_json, write_json
from scripts.common.bitbucket_api import get_pr_state
from scripts.common.config import load_config
from scripts.standup.slack_client import notify


def _call_learn(issue_id: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.jira.learn", "--issue-id", issue_id],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"learn step failed with exit code {result.returncode}")


@click.command()
@click.option("--issue-id", required=True, help="Jira issue key (e.g. PROJ-42)")
def main(issue_id: str) -> None:
    config = load_config(required=["BITBUCKET_USERNAME", "BITBUCKET_TOKEN"])
    print(f"[check-pr-state] issue={issue_id}", flush=True)

    pr = read_json(config, issue_id, "pr.json")
    if not pr:
        print("ERROR: pr.json not found", file=sys.stderr)
        sys.exit(1)

    workspace = pr.get("workspace") or config.get("BITBUCKET_WORKSPACE", "")
    repo_name = pr.get("repo") or config.get("BITBUCKET_REPO", "")
    pr_id = pr.get("id") or pr.get("url", "").rstrip("/").split("/")[-1]

    state = get_pr_state(config, workspace, repo_name, pr_id)
    print(f"[check-pr-state] PR {pr_id} state={state}", flush=True)

    if state == "UNKNOWN":
        print("ERROR: could not fetch PR state (API error or token expired)", file=sys.stderr)
        write_json(config, issue_id, "poll_state.json", {"terminal": False, "state": "UNKNOWN", "error": True})
        sys.exit(1)

    if state in ("MERGED", "DECLINED", "SUPERSEDED"):
        write_json(config, issue_id, "monitor_result.json", {"status": state, "pr_id": pr_id})
        write_json(config, issue_id, "poll_state.json", {"terminal": True, "state": state})
        try:
            _call_learn(issue_id)
        except RuntimeError as e:
            print(f"WARNING: learn step failed (non-fatal): {e}", file=sys.stderr)
        notify(config, f"✅ PR {pr_id} {state.lower()} for issue {issue_id}: {pr.get('url', '')}")
        print(f"[check-pr-state] terminal: {state}", flush=True)
        sys.exit(0)

    write_json(config, issue_id, "poll_state.json", {"terminal": False, "state": state})
    print(f"[check-pr-state] PR still open", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
