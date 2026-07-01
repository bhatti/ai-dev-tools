"""Check PR state once: respond to new comments, exit when merged/closed.

Runs once and exits — the caller (CronJob, formicary cron, shell loop) handles
polling frequency. No sleep, no loop.

Usage:
    python -m scripts.gh.poll_pr --issue-id 42

Required env: GH_ORG, GH_REPO, GH_TOKEN
Reads:  /workspace/{issue_id}/pr.json
Writes: /workspace/{issue_id}/monitor_result.json
        /workspace/{issue_id}/processed_comments.json

Exit codes: 0=merged/closed (done), 3=still open (retry later), 1=error
"""

import json
import subprocess
import sys
from pathlib import Path

import click

from scripts.common.artifacts import read_json, write_json
from scripts.common.claude_runner import run_claude
from scripts.common.config import get_issue_dir, load_config
from scripts.common.git_utils import commit_all, current_branch, push_branch


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def get_pr_state(org: str, repo: str, pr_number: int) -> str:
    """Return PR state: OPEN, MERGED, CLOSED."""
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



def fetch_new_comments(
    org: str, repo: str, pr_number: int, processed_ids: set[int]
) -> list[dict]:
    """Fetch unprocessed PR comments and review comments."""
    comments = []
    for endpoint, ctype in [
        (f"repos/{org}/{repo}/issues/{pr_number}/comments", "issue"),
        (f"repos/{org}/{repo}/pulls/{pr_number}/comments", "review"),
    ]:
        result = _run([
            "gh", "api", endpoint,
            "--jq", f"[.[] | {{id: .id, body: .body, user: .user.login, path: .path, type: \"{ctype}\"}}]",
        ], check=False)
        if result.returncode != 0:
            print(f"WARNING: gh api {endpoint} failed: {result.stderr.strip()}", file=sys.stderr)
        else:
            comments.extend(json.loads(result.stdout or "[]"))
    return [c for c in comments if c["id"] not in processed_ids]


def respond_to_comment(
    config: dict,
    issue_id: str,
    org: str,
    repo: str,
    pr_number: int,
    comment: dict,
    repo_dir: Path,
    branch: str,
) -> None:
    """Use Claude to address a single comment, then push and reply."""
    issue_dir = get_issue_dir(config, issue_id)
    prompt = f"""\
You are an AI agent responding to PR review feedback.

## Comment from @{comment['user']}
{comment['body']}

{"## File: " + comment.get('path', '') if comment.get('path') else ''}

## Instructions
1. Analyze the feedback carefully.
2. Make the requested changes to the code.
3. Run tests to verify nothing is broken.
4. Commit with: "feedback: address comment from @{comment['user']}"
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
        log_file=issue_dir / "logs" / f"feedback_{comment['id']}.log",
    )

    commit_all(repo_dir, f"feedback: address comment from @{comment['user']}")
    push_branch(repo_dir, branch, force_with_lease=True)

    reply_body = f"Addressed feedback from @{comment['user']}. {result.status_json.get('summary', '')}"
    reply = _run([
        "gh", "api",
        f"repos/{org}/{repo}/issues/{pr_number}/comments",
        "-f", f"body={reply_body}",
    ], check=False)
    if reply.returncode != 0:
        raise RuntimeError(f"Failed to post reply comment on PR #{pr_number}: {reply.stderr.strip()}")


def call_learn(issue_id: str) -> None:
    """Call learn.py as a subprocess after PR is resolved. Raises on failure."""
    result = subprocess.run(
        [sys.executable, "-m", "scripts.gh.learn", "--issue-id", issue_id],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"learn step failed with exit code {result.returncode}")


@click.command()
@click.option("--issue-id", required=True, help="Issue number")
def main(issue_id: str) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO", "GH_TOKEN"])

    pr = read_json(config, issue_id, "pr.json")
    if not pr or not pr.get("number"):
        print("ERROR: pr.json not found or missing PR number", file=sys.stderr)
        sys.exit(1)

    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    pr_number = int(pr["number"])
    branch = pr["branch"]

    issue_dir = get_issue_dir(config, issue_id)
    repo_dir = issue_dir / "repo"

    print(f"Checking PR #{pr_number}")

    state = get_pr_state(org, repo, pr_number)
    print(f"PR #{pr_number} state: {state}")

    if state == "ERROR":
        print("ERROR: could not determine PR state, skipping this poll cycle", file=sys.stderr)
        sys.exit(1)

    if state in ("MERGED", "CLOSED"):
        write_json(config, issue_id, "monitor_result.json", {
            "status": state,
            "pr_number": pr_number,
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

    all_new = fetch_new_comments(org, repo, pr_number, processed_ids)
    # Mark all seen — only act on those starting with "ai-bot"
    for c in all_new:
        processed_ids.add(c["id"])
    ai_bot_comments = [c for c in all_new if c.get("body", "").strip().lower().startswith("ai-bot")]

    if not ai_bot_comments:
        skipped = len(all_new) - len(ai_bot_comments)
        print(f"PR #{pr_number} open, no new ai-bot comments ({skipped} other comment(s) ignored)")
        if all_new:
            write_json(config, issue_id, "processed_comments.json", {"ids": list(processed_ids)})
        # Exit 3 = PR still open; formicary maps this to PAUSED and retries after delay
        sys.exit(3)

    for comment in ai_bot_comments:
        print(f"  Responding to ai-bot comment #{comment['id']} from @{comment['user']}")
        try:
            respond_to_comment(config, issue_id, org, repo, pr_number, comment, repo_dir, branch)
        except Exception as e:
            print(f"WARNING: failed to respond to comment #{comment['id']}: {e}", file=sys.stderr)
        write_json(config, issue_id, "processed_comments.json", {"ids": list(processed_ids)})

    print(f"Handled {len(ai_bot_comments)} comment(s), PR still open")
    # Exit 3 = PR still open; formicary will retry after delay
    sys.exit(3)


if __name__ == "__main__":
    main()
