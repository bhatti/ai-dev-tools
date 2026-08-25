"""Fetch new BitBucket PR comments and filter to actionable ai-bot comments.

Phase 2 of poll-pr: reads PR state from poll_state.json (skip if terminal),
fetches unprocessed comments, filters to ai-bot prefixed ones.

Usage:
    python -m scripts.jira.fetch_comments --issue-id PROJ-42

Reads:  /workspace/{issue_id}/pr.json
        /workspace/{issue_id}/poll_state.json
        /workspace/{issue_id}/processed_comments.json
Writes: /workspace/{issue_id}/processed_comments.json  (updated)
        /workspace/{issue_id}/pending_comments.json

Exit codes: 0=ok (comments found or none), 1=error
"""

import sys

import click

from scripts.common.artifacts import read_json, write_json
from scripts.common.bitbucket_api import list_pr_comments
from scripts.common.config import load_config


@click.command()
@click.option("--issue-id", required=True, help="Jira issue key (e.g. PROJ-42)")
def main(issue_id: str) -> None:
    config = load_config(required=["BITBUCKET_USERNAME", "BITBUCKET_TOKEN"])
    print(f"[fetch-comments] issue={issue_id}", flush=True)

    poll_state = read_json(config, issue_id, "poll_state.json")
    if poll_state and poll_state.get("terminal"):
        print("[fetch-comments] PR is terminal — skipping", flush=True)
        write_json(config, issue_id, "pending_comments.json", {"comments": []})
        sys.exit(0)

    pr = read_json(config, issue_id, "pr.json")
    if not pr:
        print("ERROR: pr.json not found", file=sys.stderr)
        sys.exit(1)

    workspace = pr.get("workspace") or config.get("BITBUCKET_WORKSPACE", "")
    repo_name = pr.get("repo") or config.get("BITBUCKET_REPO", "")
    pr_id = pr.get("id") or pr.get("url", "").rstrip("/").split("/")[-1]

    _raw_username = config.get("BITBUCKET_USERNAME", "")
    bot_username = config.get("BotNickname") or (
        _raw_username.split("@")[0] if "@" in _raw_username else _raw_username
    )

    processed_data = read_json(config, issue_id, "processed_comments.json") or {"ids": []}
    processed_ids = set(processed_data["ids"])

    comments = list_pr_comments(config, workspace, repo_name, pr_id)
    all_new = [c for c in comments if c.get("id") not in processed_ids]
    for c in all_new:
        processed_ids.add(c.get("id"))

    ai_bot_comments = [
        c for c in all_new
        if (c.get("content", {}).get("raw", "") or c.get("body", "")).strip().lower().startswith("ai-bot")
        and c.get("author", {}).get("nickname", "").lower() != bot_username.lower()
    ]

    write_json(config, issue_id, "processed_comments.json", {"ids": list(processed_ids)})
    write_json(config, issue_id, "pending_comments.json", {"comments": ai_bot_comments})

    skipped = len(all_new) - len(ai_bot_comments)
    print(f"[fetch-comments] new={len(all_new)} actionable={len(ai_bot_comments)} skipped={skipped}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
