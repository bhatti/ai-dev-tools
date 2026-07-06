"""Check a BitBucket PR state once: respond to new comments, exit when resolved.

Runs once and exits — caller handles polling frequency.

Usage:
    python -m scripts.jira.poll_pr --issue-id PROJ-42

Required env: BITBUCKET_USERNAME, BITBUCKET_TOKEN (or from pr.json)
Reads:  /workspace/pr.json
Writes: /workspace/monitor_result.json
        /workspace/processed_comments.json

Exit codes: 0=merged/declined (done), 3=still open (retry later), 1=error
"""

import subprocess
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
from scripts.common.config import get_issue_dir, load_config, validate_claude_config
from scripts.common.git_utils import clone_repo, commit_all, configure_git, create_branch, detect_bitbucket_url, push_branch
from scripts.common.shell import run_cmd as _run


def ensure_repo_clone(
    config: dict,
    workspace: str,
    repo_name: str,
    branch: str,
    repo_dir: Path,
) -> None:
    """Clone the BitBucket repo and checkout the feature branch with remote tracking.

    A shallow clone only fetches the default branch. We explicitly fetch the
    feature branch so that --force-with-lease works correctly.
    """
    http_token = config.get("BITBUCKET_TOKEN", "")
    ssh_key = config.get("SSH_PRIVATE_KEY", "")
    http_username = config.get("BITBUCKET_USERNAME", "x-token-auth")

    if http_token:
        clone_url = detect_bitbucket_url(workspace, repo_name, use_ssh=False)
    else:
        clone_url = detect_bitbucket_url(workspace, repo_name, use_ssh=True)

    if not (repo_dir.exists() and (repo_dir / ".git").exists()):
        print(f"Cloning {workspace}/{repo_name} branch={branch} for feedback response")
        if http_token:
            clone_repo(clone_url, repo_dir, http_token=http_token, http_username=http_username)
        else:
            clone_repo(clone_url, repo_dir, ssh_key=ssh_key)
        configure_git(
            repo_dir,
            config.get("GIT_USER_NAME", "AI Agent"),
            config.get("GIT_USER_EMAIL", "ai-agent@noreply.local"),
        )

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

    # checkout the branch tracking origin/<branch>
    create_branch(repo_dir, branch)


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
    http_token = config.get("BITBUCKET_TOKEN", "")
    http_username = config.get("BITBUCKET_USERNAME", "x-token-auth")
    author = comment.get("author", {}).get("nickname", "unknown")
    body = comment.get("content", {}).get("raw", "") or comment.get("body", "")
    comment_id = comment.get("id")

    max_turns = int(config.get("MAX_TURNS_FEEDBACK", "10"))
    prompt = f"""\
You are an AI agent responding to BitBucket PR review feedback.

## Comment from @{author}
{body}

## Instructions
1. Read CLAUDE.md or any repo-specific coding guidelines if they exist and follow them.
2. Analyze the feedback carefully.
3. Make the requested changes — edit the file directly. Keep changes minimal and focused.
4. Do NOT run tests or lint — just make the change and commit.
5. Commit with: "feedback: address comment from @{author}"
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
        log_file=issue_dir / "logs" / f"feedback_{comment_id}.log",
    )
    commit_all(repo_dir, f"feedback: address comment from @{author}")

    # Re-fetch the tracking ref before pushing so --force-with-lease has an up-to-date
    # lease value. Claude may have pushed its own commits via Bash during its run.
    refspec = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
    fetch_result = _run(["git", "-C", str(repo_dir), "fetch", "--depth", "100", "origin", refspec], check=False)
    if fetch_result.returncode != 0:
        print(f"WARNING: pre-push fetch failed: {fetch_result.stderr.strip()}", file=sys.stderr)

    # Refresh credentials in remote URL before push — handles re-runs where the
    # repo already exists and clone_repo was not called this invocation.
    if http_token:
        push_url = detect_bitbucket_url(workspace, repo_name, use_ssh=False)
        push_branch(repo_dir, branch, force_with_lease=True,
                    http_token=http_token, http_username=http_username, url=push_url)
    else:
        push_branch(repo_dir, branch, force_with_lease=True)

    summary = (result.status_json or {}).get("summary", "")
    reply = f"Addressed feedback from @{author}. {summary}".strip()
    add_pr_comment(config, workspace, repo_name, pr_id, reply)


def call_learn(issue_id: str) -> None:
    """Call jira learn.py as a subprocess after PR is resolved."""
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
    validate_claude_config(config)
    print(f"[poll_pr] issue={issue_id}", flush=True)

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
    # BITBUCKET_USERNAME is the email for API auth; the Bitbucket API returns
    # author nicknames. Use a dedicated BotNickname config if set, otherwise
    # fall back to the part before '@' in the email.
    _raw_username = config.get("BITBUCKET_USERNAME", "")
    bot_username = config.get("BotNickname") or (
        _raw_username.split("@")[0] if "@" in _raw_username else _raw_username
    )

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
    # Mark all seen before processing so we don't double-process on next run
    for c in all_new:
        processed_ids.add(c.get("id"))

    # Only respond to comments prefixed with "ai-bot" — skip bot's own replies
    ai_bot_comments = [
        c for c in all_new
        if (c.get("content", {}).get("raw", "") or c.get("body", "")).strip().lower().startswith("ai-bot")
        and c.get("author", {}).get("nickname", "").lower() != bot_username.lower()
    ]

    if not ai_bot_comments:
        skipped = len(all_new) - len(ai_bot_comments)
        print(f"PR {pr_id} open, no new ai-bot comments ({skipped} other comment(s) ignored)")
        if all_new:
            write_json(config, issue_id, "processed_comments.json", {"ids": list(processed_ids)})
        sys.exit(3)

    # Clone repo on demand — poll-pr runs in a fresh pod with emptyDir
    ensure_repo_clone(config, workspace, repo_name, branch, repo_dir)

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
    print(f"Handled {len(ai_bot_comments)} comment(s), PR still open")
    sys.exit(3)


if __name__ == "__main__":
    main()
