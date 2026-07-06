"""Check PR state once: respond to new comments, exit when merged/closed.

Runs once and exits — the caller (CronJob, formicary cron, shell loop) handles
polling frequency. No sleep, no loop.

Usage:
    python -m scripts.gh.poll_pr --issue-id 42

Required env: GH_ORG, GH_REPO, GH_TOKEN
Reads:  /workspace/pr.json
        /workspace/impl_result.json  (for branch name and repo URL)
Writes: /workspace/monitor_result.json
        /workspace/processed_comments.json

Exit codes: 0=merged/closed (done), 3=still open (retry later), 1=error
"""

import json
import subprocess
import sys
from pathlib import Path

import click

from scripts.common.artifacts import read_json, write_json
from scripts.common.claude_runner import run_claude
from scripts.common.config import get_issue_dir, load_config, validate_claude_config
from scripts.common.git_utils import clone_repo, commit_all, configure_git, create_branch, push_branch
from scripts.common.shell import run_cmd as _run


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
    """Fetch unprocessed PR issue comments and review comments."""
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


def ensure_repo_clone(
    config: dict,
    org: str,
    repo: str,
    branch: str,
    repo_dir: Path,
) -> None:
    """Clone the repo and checkout the feature branch with full remote tracking.

    A shallow clone only fetches the default branch. We explicitly fetch the
    feature branch so that --force-with-lease works (it needs the remote
    tracking ref to know the expected remote state).
    """
    token = config.get("GH_TOKEN", "")
    if token:
        clone_url = f"https://x-access-token:{token}@github.com/{org}/{repo}.git"
    else:
        clone_url = f"git@github.com:{org}/{repo}.git"

    if not (repo_dir.exists() and (repo_dir / ".git").exists()):
        print(f"Cloning {org}/{repo} branch={branch} for feedback response")
        clone_repo(clone_url, repo_dir)
        configure_git(repo_dir, config.get("GIT_USER_NAME", "AI Agent"), config.get("GIT_USER_EMAIL", "ai-agent@noreply.local"))

    # Fetch the feature branch with an explicit refspec so git creates the local
    # tracking ref refs/remotes/origin/<branch>.  A bare `git fetch origin <branch>`
    # only writes FETCH_HEAD — it does NOT create the tracking ref, which makes
    # `--force-with-lease` refuse the push (no lease value to compare against).
    print(f"Fetching branch {branch}")
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    fetch = _run(
        ["git", "-C", str(repo_dir), "fetch", "--depth", "100", "origin", refspec],
        check=False,
    )
    if fetch.returncode != 0:
        raise RuntimeError(f"git fetch failed for branch {branch}: {fetch.stderr.strip() or fetch.stdout.strip()}")

    # Set the remote URL with credentials so the subsequent push can authenticate.
    if token:
        _run(
            ["git", "-C", str(repo_dir), "remote", "set-url", "origin", clone_url],
            check=False,
        )

    # checkout the branch, tracking origin/<branch>
    create_branch(repo_dir, branch)


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
    """Use Claude to address a single comment, commit, push, and post a reply."""
    issue_dir = get_issue_dir(config, issue_id)
    max_turns = int(config.get("MAX_TURNS_FEEDBACK", "10"))
    prompt = f"""\
You are an AI agent responding to a PR review comment.

## Comment from @{comment['user']}
{comment['body']}

{"## File: " + comment.get('path', '') if comment.get('path') else ''}

## Instructions
1. Read CLAUDE.md or any repo-specific coding guidelines if they exist and follow them.
2. Analyze the feedback carefully.
3. Make the requested changes — edit the file directly. Keep changes minimal and focused.
4. Do NOT run tests or lint — just make the change and commit.
5. Commit with: "feedback: address comment from @{comment['user']}"
6. Output ONLY this JSON on the last line:
   {{"status":"DONE","commits":<N>,"summary":"<one sentence>"}}
   Or if you cannot address it:
   {{"status":"SKIPPED","reason":"<explanation>"}}
"""
    result = run_claude(
        prompt,
        working_dir=repo_dir,
        model=config.get("AI_MODEL"),
        max_turns=max_turns,
        log_file=issue_dir / "logs" / f"feedback_{comment['id']}.log",
    )

    commit_all(repo_dir, f"feedback: address comment from @{comment['user']}")

    # Re-fetch the tracking ref before pushing so --force-with-lease has an
    # up-to-date lease value. Claude may have pushed its own commits during its
    # run via Bash tool, advancing the remote past our local tracking ref.
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    fetch_result = _run(["git", "-C", str(repo_dir), "fetch", "--depth", "100", "origin", refspec], check=False)
    if fetch_result.returncode != 0:
        print(f"WARNING: pre-push fetch failed: {fetch_result.stderr.strip()}", file=sys.stderr)

    local_tip = _run(["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"], check=False)
    remote_tip = _run(["git", "-C", str(repo_dir), "rev-parse", "--short", f"refs/remotes/origin/{branch}"], check=False)
    print(f"  Push: local={local_tip.stdout.strip()} remote={remote_tip.stdout.strip()}")

    # Do NOT pass http_token/url here — credentials are already embedded in the
    # remote URL by ensure_repo_clone. Passing them again causes push_branch to
    # call `git remote set-url`, which invalidates the tracking refs and makes
    # --force-with-lease report "stale info" even when SHAs match.
    push_branch(repo_dir, branch, force_with_lease=True)

    summary = (result.status_json or {}).get("summary", "")
    reply_body = f"Addressed feedback from @{comment['user']}. {summary}".strip()
    reply = _run([
        "gh", "api",
        f"repos/{org}/{repo}/issues/{pr_number}/comments",
        "-f", f"body={reply_body}",
    ], check=False)
    if reply.returncode != 0:
        raise RuntimeError(f"Failed to post reply comment on PR #{pr_number}: {reply.stderr.strip()}")


def call_learn(issue_id: str) -> None:
    """Call learn.py as a subprocess after PR is resolved. Non-fatal on failure."""
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
    validate_claude_config(config)
    print(f"[poll_pr] issue={issue_id} org={config['GH_ORG']} repo={config['GH_REPO']}", flush=True)

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

    print(f"Checking PR #{pr_number} branch={branch}")

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
    # Mark all fetched comments as seen before processing so we don't double-process
    for c in all_new:
        processed_ids.add(c["id"])

    # Only respond to comments prefixed with "ai-bot" — other comments are human discussion
    # that doesn't require an automated code change. Also skip the bot's own replies.
    bot_usernames = {"github-actions[bot]", "ai-agent", "ai-bot"}
    actionable = [
        c for c in all_new
        if c.get("body", "").strip().lower().startswith("ai-bot")
        and c.get("user", "").lower() not in bot_usernames
    ]

    if not actionable:
        print(f"PR #{pr_number} open, no new 'ai-bot' comments to action ({len(all_new)} seen, {len(all_new) - len(actionable)} skipped)")
        if all_new:
            write_json(config, issue_id, "processed_comments.json", {"ids": list(processed_ids)})
        # Exit 3 = PR still open; formicary maps to PAUSED and retries after delay
        sys.exit(3)

    # Clone repo on demand — poll-pr runs in a fresh pod with emptyDir
    ensure_repo_clone(config, org, repo, branch, repo_dir)

    for comment in actionable:
        print(f"  Responding to comment #{comment['id']} from @{comment['user']}")
        try:
            respond_to_comment(config, issue_id, org, repo, pr_number, comment, repo_dir, branch)
        except Exception as e:
            import subprocess as _sp
            extra = ""
            if isinstance(e, _sp.CalledProcessError):
                extra = f"\n  stdout: {e.stdout!r}\n  stderr: {e.stderr!r}"
            print(f"WARNING: failed to respond to comment #{comment['id']}: {e}{extra}", file=sys.stderr)

    write_json(config, issue_id, "processed_comments.json", {"ids": list(processed_ids)})
    print(f"Handled {len(actionable)} comment(s), PR still open")
    # Exit 3 = PR still open; formicary will retry after delay
    sys.exit(3)


if __name__ == "__main__":
    main()
