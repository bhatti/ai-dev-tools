"""Create a GitHub Pull Request (branch is already pushed by implement step).

Wrapper that calls build_pr → notify_pr in sequence.
Each sub-module is independently re-runnable.

Usage:
    python -m scripts.gh.create_pr --issue-id 42

Exit codes: 0=success, 1=error
"""

import sys

import click

from scripts.gh.build_pr import main as build_pr_main
from scripts.gh.notify_pr import main as notify_pr_main


@click.command()
@click.option("--issue-id", required=True, help="GitHub issue number")
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
