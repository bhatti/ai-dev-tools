"""Fetch new GitHub PR comments and filter to actionable bot comments.

Phase 2 of poll-pr: reads poll_state.json (skip if terminal), fetches
unprocessed PR issue + review comments, filters to ones whose first word ends with "bot".

Usage:
    python -m scripts.gh.fetch_comments --issue-id 42

Reads:  /workspace/{issue_id}/pr.json
        /workspace/{issue_id}/poll_state.json
        /workspace/{issue_id}/processed_comments.json
Writes: /workspace/{issue_id}/processed_comments.json  (updated)
        /workspace/{issue_id}/pending_comments.json

Exit codes: 0=ok, 1=error
"""

import json
import sys

import click

from scripts.common.artifacts import read_json, write_json
from scripts.common.config import load_config
from scripts.common.pr_utils import REPLIED_TO_RE, is_bot_trigger
from scripts.common.shell import run_cmd as _run


_BOT_USERNAMES = {"github-actions[bot]", "ai-agent", "ai-bot"}


def _fetch_all_comments(org: str, repo: str, pr_number: int) -> list:
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
    return comments


@click.command()
@click.option("--issue-id", required=True, help="GitHub issue number")
def main(issue_id: str) -> None:
    config = load_config(required=["GH_ORG", "GH_REPO"])
    print(f"[fetch-comments] issue={issue_id}", flush=True)

    poll_state = read_json(config, issue_id, "poll_state.json")
    if poll_state and poll_state.get("terminal"):
        print("[fetch-comments] PR is terminal — skipping", flush=True)
        write_json(config, issue_id, "pending_comments.json", {"comments": []})
        sys.exit(0)

    pr = read_json(config, issue_id, "pr.json")
    if not pr or not pr.get("number"):
        print("ERROR: pr.json not found or missing PR number", file=sys.stderr)
        sys.exit(1)

    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    pr_number = int(pr["number"])

    processed_data = read_json(config, issue_id, "processed_comments.json") or {"ids": []}
    processed_ids = set(processed_data["ids"])

    all_comments = _fetch_all_comments(org, repo, pr_number)
    all_new = [c for c in all_comments if c["id"] not in processed_ids]
    for c in all_new:
        processed_ids.add(c["id"])

    # Build set of comment IDs already replied to (scan bot replies for explicit marker)
    bot_usernames_lower = {u.lower() for u in _BOT_USERNAMES}
    already_replied_ids: set[int] = set()
    for c in all_comments:
        if c.get("user", "").lower() in bot_usernames_lower:
            for m in REPLIED_TO_RE.finditer(c.get("body", "")):
                already_replied_ids.add(int(m.group(1)))

    # Filter: first word must end with "bot"
    actionable = [
        c for c in all_new
        if is_bot_trigger(c.get("body", ""))
        and c.get("user", "").lower() not in bot_usernames_lower
        and c["id"] not in already_replied_ids
    ]

    write_json(config, issue_id, "processed_comments.json", {"ids": list(processed_ids)})
    write_json(config, issue_id, "pending_comments.json", {"comments": actionable})

    skipped = len(all_new) - len(actionable)
    print(f"[fetch-comments] new={len(all_new)} actionable={len(actionable)} skipped={skipped}", flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
