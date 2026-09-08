"""Clone GitHub repo and create feature branch for a GitHub issue.

Usage:
    python -m scripts.gh.clone_repo --issue-id 42

Reads:  /workspace/{issue_id}/issue.json
        /workspace/{issue_id}/plan.md
Writes: /workspace/{issue_id}/branch.txt
        /workspace/{issue_id}/repo/
        /workspace/{issue_id}/clone_result.json

Exit codes: 0=done, 1=error
"""

import sys

import click

from scripts.common.artifacts import read_json, read_text, write_json
from scripts.common.config import get_issue_dir, load_config
from scripts.common.skills import apply_project_skills
from scripts.common.git_utils import (
    clone_by_tracker,
    configure_git,
    create_branch,
    make_branch_name,
)


@click.command()
@click.option("--issue-id", required=True, help="GitHub issue number")
def main(issue_id: str) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO"])
    print(f"[clone-repo] issue={issue_id} org={config['GH_ORG']} repo={config['GH_REPO']}", flush=True)

    issue_dir = get_issue_dir(config, issue_id)

    branch_file = issue_dir / "branch.txt"
    repo_dir = issue_dir / "repo"
    if branch_file.exists() and repo_dir.exists():
        branch = branch_file.read_text().strip()
        print(f"[clone-repo] repo already cloned, branch={branch} — skipping", flush=True)
        sys.exit(0)

    issue = read_json(config, issue_id, "issue.json")
    if not issue:
        print(f"ERROR: {issue_dir}/issue.json not found", file=sys.stderr)
        sys.exit(1)

    plan = read_text(config, issue_id, "plan.md")
    if not plan:
        print(f"ERROR: {issue_dir}/plan.md not found", file=sys.stderr)
        sys.exit(1)

    # Override GH_REPO if the issue specifies a different repo
    issue_repo = issue.get("repo")
    if issue_repo:
        config = {**config, "GH_REPO": issue_repo}
    clone_by_tracker(config, repo_dir, tracker="github")

    configure_git(repo_dir, config["GIT_USER_NAME"], config["GIT_USER_EMAIL"])

    branch = make_branch_name(issue_id, issue["title"])
    branch_file.write_text(branch)

    base_branch = config.get("BASE_BRANCH", "main")
    print(f"[clone-repo] branch={branch} base={base_branch}", flush=True)
    create_branch(repo_dir, branch, base_branch=base_branch)

    apply_project_skills(repo_dir)
    write_json(config, issue_id, "clone_result.json", {"status": "DONE", "branch": branch})
    print(f"[clone-repo] done: branch={branch}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
