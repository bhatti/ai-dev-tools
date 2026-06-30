"""Check a BitBucket PR state once: respond to new comments, exit when resolved.

Runs once and exits — caller handles polling frequency.

Usage:
    python -m scripts.jira.poll_pr --issue-id PROJ-42

Required env: BITBUCKET_USERNAME, BITBUCKET_TOKEN (or from pr.json)
Reads:  /workspace/{issue_id}/pr.json
Writes: /workspace/{issue_id}/monitor_result.json
        /workspace/{issue_id}/processed_comments.json

Exit codes: 0=merged/declined or comments handled, 1=error
"""

import sys
from pathlib import Path

import click

from scripts.common.artifacts import read_json, write_json
from scripts.common.bitbucket_api import (
    add_pr_comment,
    get_pr_state,
    list_pr_comments,
)
from scripts.common.claude_runner import run_claude
from scripts.common.config import get_issue_dir, load_config
from scripts.common.git_utils import commit_all, push_branch


def respond_to_comment(
    config: dict,
    issue_id: str,
    workspace: str,
    repo_name: str,
    pr_id: int,
    comment: dict,
    repo_dir: Path,
    branch: str,
) -> None:
    issue_dir = get_issue_dir(config, issue_id)
    author = comment.get("author", {}).get("nickname", "unknown")
    body = comment.get("content", {}).get("raw", "") or comment.get("body", "")
    comment_id = comment.get("id")

    prompt = f"""\
You are an AI agent responding to BitBucket PR review feedback.

## Comment from @{author}
{body}

## Instructions
1. Analyze the feedback carefully.
2. Make the requested changes to the code.
3. Run tests to verify nothing is broken.
4. Commit with: "feedback: address comment from @{author}"
5. Output ONLY this JSON on the last line:
   {{"status":"DONE","commits":<N>,"summary":"<one sentence>"}}
   Or if you cannot address it:
   {{"status":"SKIPPED","reason":"<explanation>"}}
"""
    result = run_claude(
        prompt,
        working_dir=repo_dir,
        model=config.get("AI_MODEL"),
        max_turns=20,
        log_file=issue_dir / "logs" / f"feedback_{comment_id}.log",
    )
    commit_all(repo_dir, f"feedback: address comment from @{author}")
    push_branch(repo_dir, branch, force_with_lease=True)

    reply = f"Addressed feedback from @{author}. {result.status_json.get('summary', '')}"
    add_pr_comment(config, workspace, repo_name, pr_id, reply)


def call_learn(issue_id: str) -> None:
    """Call jira learn.py as a subprocess after PR is resolved."""
    import subprocess
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

    pr = read_json(config, issue_id, "pr.json")
    if not pr:
        print("ERROR: pr.json not found", file=sys.stderr)
        sys.exit(1)

    workspace = pr.get("workspace") or config.get("BITBUCKET_WORKSPACE", "")
    repo_name = pr.get("repo") or config.get("BITBUCKET_REPO", "")
    pr_id = pr.get("id")
    if not pr_id:
        pr_id = pr.get("url", "").rstrip("/").split("/")[-1]
    branch = pr["branch"]
    issue_dir = get_issue_dir(config, issue_id)
    repo_dir = issue_dir / "repo"
    bot_username = config.get("BITBUCKET_USERNAME", "")

    print(f"Checking BitBucket PR {pr_id} as @{bot_username}")

    state = get_pr_state(config, workspace, repo_name, pr_id)
    print(f"PR {pr_id} state: {state}")

    if state in ("MERGED", "DECLINED"):
        write_json(config, issue_id, "monitor_result.json", {
            "status": state,
            "pr_id": pr_id,
        })
        print(f"PR {state} — running learn step")
        try:
            call_learn(issue_id)
        except RuntimeError as e:
            print(f"WARNING: learn step failed (non-fatal): {e}", file=sys.stderr)
        print("Done")
        sys.exit(0)

    processed_data = read_json(config, issue_id, "processed_comments.json") or {"ids": []}
    processed_ids = set(processed_data["ids"])

    comments = list_pr_comments(config, workspace, repo_name, pr_id)
    all_new = [c for c in comments if c.get("id") not in processed_ids]
    # Mark all seen — only act on those starting with "ai-bot"
    for c in all_new:
        processed_ids.add(c.get("id"))
    ai_bot_comments = [
        c for c in all_new
        if (c.get("content", {}).get("raw", "") or c.get("body", "")).strip().lower().startswith("ai-bot")
    ]

    if not ai_bot_comments:
        skipped = len(all_new) - len(ai_bot_comments)
        print(f"PR {pr_id} open, no new ai-bot comments ({skipped} other comment(s) ignored)")
        if all_new:
            write_json(config, issue_id, "processed_comments.json", {"ids": list(processed_ids)})
        sys.exit(0)

    for comment in ai_bot_comments:
        comment_id = comment.get("id")
        author = comment.get("author", {}).get("nickname", "unknown")
        print(f"  Responding to ai-bot comment {comment_id} from @{author}")
        try:
            respond_to_comment(
                config, issue_id, workspace, repo_name, pr_id, comment, repo_dir, branch
            )
        except Exception as e:
            print(f"WARNING: failed to respond to comment {comment_id}: {e}", file=sys.stderr)
        write_json(config, issue_id, "processed_comments.json", {"ids": list(processed_ids)})

    print(f"Handled {len(ai_bot_comments)} comment(s)")
    sys.exit(0)


if __name__ == "__main__":
    main()
