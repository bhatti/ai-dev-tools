"""Push branch and create a BitBucket Pull Request via REST API.

Wrapper that calls build_pr → notify_pr in sequence.
Each sub-module is independently re-runnable.

Usage:
    python -m scripts.jira.create_pr --issue-id PROJ-42

Exit codes: 0=success, 1=error
"""

import sys

import click

from scripts.jira.build_pr import main as build_pr_main
from scripts.jira.notify_pr import main as notify_pr_main


@click.command()
@click.option("--issue-id", required=True, help="Jira issue key (e.g. PROJ-42)")
def main(issue_id: str) -> None:
    for step, cmd in [
        ("build-pr", build_pr_main),
        ("notify-pr", notify_pr_main),
    ]:
        try:
            cmd.main(["--issue-id", issue_id], standalone_mode=False)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if code != 0:
                print(f"[create-pr] step={step} exited with code {code}", flush=True)
                sys.exit(code)


if __name__ == "__main__":
    main()
