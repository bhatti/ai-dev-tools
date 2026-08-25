"""Implement a Jira issue: clone repo → run Claude → push branch.

Wrapper that calls clone_repo → run_implement → push_impl in sequence.
Each sub-module is idempotent and can be re-run independently.

Usage:
    python -m scripts.jira.implement --issue-id PROJ-42

Exit codes: 0=done, 2=blocked, 1=error/tests-failing
"""

import sys

import click

from scripts.jira.clone_repo import main as clone_repo_main
from scripts.jira.run_implement import main as run_implement_main
from scripts.jira.push_impl import main as push_impl_main


@click.command()
@click.option("--issue-id", required=True, help="Jira issue key (e.g. PROJ-42)")
def main(issue_id: str) -> None:
    # Each sub-command calls sys.exit — invoke via standalone_mode=False to catch exit codes.
    for step, cmd in [
        ("clone-repo", clone_repo_main),
        ("run-implement", run_implement_main),
        ("push-impl", push_impl_main),
    ]:
        try:
            cmd.main(["--issue-id", issue_id], standalone_mode=False)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if code != 0:
                print(f"[implement] step={step} exited with code {code}", flush=True)
                sys.exit(code)


if __name__ == "__main__":
    main()
