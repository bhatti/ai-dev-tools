"""Check GitHub PR state once: respond to new comments, exit when merged/closed.

Wrapper that calls check_pr_state → fetch_comments → respond_comments in sequence.
Each sub-module is independently re-runnable.

Usage:
    python -m scripts.gh.poll_pr --issue-id 42

Exit codes: 0=merged/closed (done), 3=still open (retry later), 1=error
"""

import sys

import click

from scripts.gh.check_pr_state import main as check_pr_state_main
from scripts.gh.fetch_comments import main as fetch_comments_main
from scripts.gh.respond_comments import main as respond_comments_main


@click.command()
@click.option("--issue-id", required=True, help="GitHub issue number")
def main(issue_id: str) -> None:
    for step, cmd in [
        ("check-pr-state", check_pr_state_main),
        ("fetch-comments", fetch_comments_main),
        ("respond-comments", respond_comments_main),
    ]:
        try:
            cmd.main(["--issue-id", issue_id], standalone_mode=False)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            # 0 after check-pr-state: read poll_state.json to decide if terminal
            if code == 0 and step == "check-pr-state":
                from scripts.common.artifacts import read_json
                from scripts.common.config import load_config
                config = load_config(required=[])
                poll_state = read_json(config, issue_id, "poll_state.json") or {}
                if poll_state.get("terminal"):
                    sys.exit(0)
                continue
            if code == 0:
                continue
            # 3 = still open; propagate to caller (PAUSE_JOB in formicary)
            sys.exit(code)


if __name__ == "__main__":
    main()
