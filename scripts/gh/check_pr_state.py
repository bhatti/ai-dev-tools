"""Check GitHub PR state and run learn/notify if terminal.

Phase 1 of poll-pr: determines PR state, handles terminal states (MERGED/CLOSED).
Writes poll_state.json so downstream phases know whether to proceed.

Usage:
    python -m scripts.gh.check_pr_state --issue-id 42

Reads:  /workspace/{issue_id}/pr.json
Writes: /workspace/{issue_id}/monitor_result.json  (only if terminal)
        /workspace/{issue_id}/poll_state.json

Exit codes: 0=ok, 1=error
"""

import json
import subprocess
import sys

import click

from scripts.common.artifacts import read_json, write_json
from scripts.common.config import load_config
from scripts.common.shell import run_cmd as _run
from scripts.standup.slack_client import notify


def _get_pr_state(org: str, repo: str, pr_number: int) -> str:
    result = _run([
        "gh", "pr", "view", str(pr_number),
        "-R", f"{org}/{repo}",
        "--json", "state,mergedAt",
    ], check=False)
    if result.returncode != 0:
        print(f"WARNING: could not fetch PR state: {result.stderr.strip()}", file=sys.stderr)
        return "ERROR"
    data = json.loads(result.stdout)
    if data.get("mergedAt"):
        return "MERGED"
    return data.get("state", "OPEN").upper()


def _call_learn(issue_id: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "scripts.gh.learn", "--issue-id", issue_id],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"learn step failed with exit code {result.returncode}")


@click.command()
@click.option("--issue-id", required=True, help="GitHub issue number")
def main(issue_id: str) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO", "GH_TOKEN"])
    print(f"[check-pr-state] issue={issue_id} org={config['GH_ORG']} repo={config['GH_REPO']}", flush=True)

    pr = read_json(config, issue_id, "pr.json")
    if not pr or not pr.get("number"):
        print("ERROR: pr.json not found or missing PR number", file=sys.stderr)
        sys.exit(1)

    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    pr_number = int(pr["number"])

    state = _get_pr_state(org, repo, pr_number)
    print(f"[check-pr-state] PR #{pr_number} state={state}", flush=True)

    if state == "ERROR":
        print("ERROR: could not determine PR state", file=sys.stderr)
        write_json(config, issue_id, "poll_state.json", {"terminal": False, "state": "ERROR", "error": True})
        sys.exit(1)

    if state in ("MERGED", "CLOSED"):
        write_json(config, issue_id, "monitor_result.json", {"status": state, "pr_number": pr_number})
        write_json(config, issue_id, "poll_state.json", {"terminal": True, "state": state})
        try:
            _call_learn(issue_id)
        except RuntimeError as e:
            print(f"WARNING: learn step failed (non-fatal): {e}", file=sys.stderr)
        notify(config, f"✅ PR #{pr_number} {state.lower()} for issue #{issue_id}: {pr.get('url', '')}")
        print(f"[check-pr-state] terminal: {state}", flush=True)
        sys.exit(0)

    write_json(config, issue_id, "poll_state.json", {"terminal": False, "state": state})
    print(f"[check-pr-state] PR still open", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
