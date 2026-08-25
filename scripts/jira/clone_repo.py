"""Clone Bitbucket repo and create feature branch for a Jira issue.

Usage:
    python -m scripts.jira.clone_repo --issue-id PROJ-42

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
    clone_repo,
    configure_git,
    create_branch,
    detect_bitbucket_url,
    make_branch_name,
)


@click.command()
@click.option("--issue-id", required=True, help="Jira issue key (e.g. PROJ-42)")
def main(issue_id: str) -> None:
    config = load_config(required=["BITBUCKET_WORKSPACE", "BITBUCKET_REPO"])
    print(f"[clone-repo] issue={issue_id}", flush=True)

    issue_dir = get_issue_dir(config, issue_id)

    # Idempotency: if branch already exists and repo dir is present, skip clone
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

    workspace = issue.get("bitbucket_workspace") or config.get("BITBUCKET_WORKSPACE", "")
    repo_name = issue.get("bitbucket_repo") or config.get("BITBUCKET_REPO", "")
    if not workspace or not repo_name:
        print("ERROR: BITBUCKET_WORKSPACE and BITBUCKET_REPO must be set", file=sys.stderr)
        sys.exit(1)

    http_token = config.get("BITBUCKET_TOKEN", "")
    ssh_key = config.get("SSH_PRIVATE_KEY", "")

    if http_token:
        http_username = config.get("BITBUCKET_USERNAME", "x-token-auth")
        clone_url = detect_bitbucket_url(workspace, repo_name, use_ssh=False)
        print(f"[clone-repo] cloning {workspace}/{repo_name} via HTTPS", flush=True)
        clone_repo(clone_url, repo_dir, http_token=http_token, http_username=http_username)
    else:
        clone_url = detect_bitbucket_url(workspace, repo_name, use_ssh=True)
        print(f"[clone-repo] cloning {workspace}/{repo_name} via SSH", flush=True)
        clone_repo(clone_url, repo_dir, ssh_key=ssh_key)

    configure_git(repo_dir, config["GIT_USER_NAME"], config["GIT_USER_EMAIL"])

    branch = make_branch_name(issue_id.replace("/", "-"), issue["title"])
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
