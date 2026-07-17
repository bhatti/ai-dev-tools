"""Gather standup signals from GitHub (via gh CLI) and optionally Slack.

Usage:
    python -m scripts.standup.gather_gh

Required env: GH_ORG, GH_REPO, GH_TOKEN
Optional env:
    SLACK_BOT_TOKEN, SLACK_STANDUP_CHANNEL (default: standup)
    STANDUP_TEAM_MEMBERS  comma-separated GitHub logins to scope brief;
                          default is all assignees with open issues
    STANDUP_LOOKBACK_HOURS   hours of history to consider (default: 26)
    STANDUP_STALE_DAYS       days without update before an issue is stale (default: 2)

Writes:
    /workspace/signals.json         raw gathered data consumed by synthesize.py
    /workspace/gather_result.json   {"status":"DONE",...} or {"status":"ERROR",...}

Exit codes: 0=done, 1=error
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta

from scripts.common.config import load_config, get_workspace_dir
from scripts.common.shell import run_cmd as _run
from scripts.standup.slack_client import get_standup_messages


def _parse_team_filter(config: dict) -> list[str]:
    return [m.strip() for m in config.get("STANDUP_TEAM_MEMBERS", "").split(",") if m.strip()]


# ---------------------------------------------------------------------------
# GitHub helpers (gh CLI)
# ---------------------------------------------------------------------------

def get_open_issues(config: dict) -> list[dict]:
    """Return open issues for the repo. Fetches all then post-filters by team.

    We always fetch all open issues (up to 200) rather than filtering by
    --assignee in the gh CLI, because --assignee accepts only a single login;
    using it for the first team member would silently drop every other member's
    issues before the post-filter could run.
    """
    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    result = _run([
        "gh", "issue", "list",
        "-R", f"{org}/{repo}",
        "--state", "open",
        "--limit", "200",
        "--json", "number,title,body,url,labels,assignees,updatedAt,createdAt,comments",
    ])
    if not result.stdout.strip():
        return []
    try:
        issues = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"[gather_gh] failed to parse gh output: {e}", file=sys.stderr)
        return []

    team_filter = _parse_team_filter(config)
    if team_filter:
        issues = [
            i for i in issues
            if any(a["login"] in team_filter for a in i.get("assignees", []))
        ]
    return issues


def get_open_prs(config: dict) -> list[dict]:
    """Return open PRs (up to 100) with age and reviewers. Uses gh CLI."""
    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    result = _run([
        "gh", "pr", "list",
        "-R", f"{org}/{repo}",
        "--state", "open",
        "--limit", "100",
        "--json", "number,title,author,createdAt,reviews,reviewRequests,url,headRefName",
    ])
    if not result.stdout.strip():
        return []
    try:
        raw_prs = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    prs = []
    for pr in raw_prs:
        created = pr.get("createdAt", "")
        try:
            age_hours = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(created.replace("Z", "+00:00"))
            ).total_seconds() / 3600
        except (ValueError, AttributeError):
            age_hours = 0

        reviewers = [r["login"] for r in pr.get("reviewRequests", [])]
        review_states = [r.get("state", "") for r in pr.get("reviews", [])]
        has_approval = "APPROVED" in review_states

        prs.append({
            "id": pr["number"],
            "title": pr.get("title", ""),
            "author": pr.get("author", {}).get("login", "unknown"),
            "created": created,
            "age_hours": round(age_hours, 1),
            "reviewers": reviewers,
            "has_approval": has_approval,
            "review_states": review_states,
            "url": pr.get("url", ""),
            "branch": pr.get("headRefName", ""),
        })
    return prs


# ---------------------------------------------------------------------------
# Issue normalisation
# ---------------------------------------------------------------------------

def _normalise_issue(raw: dict, stale_cutoff: datetime, lookback_cutoff: datetime | None = None) -> dict:
    if lookback_cutoff is None:
        lookback_cutoff = datetime.now(timezone.utc) - timedelta(hours=26)
    updated_str = raw.get("updatedAt", "")
    try:
        updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        stale_days_count = (datetime.now(timezone.utc) - updated).days
        is_stale = updated < stale_cutoff
    except (ValueError, AttributeError):
        stale_days_count = 0
        is_stale = False

    label_names = [lbl["name"] for lbl in raw.get("labels", [])]
    is_blocked = any("blocked" in lbl.lower() for lbl in label_names)
    assignees = [a["login"] for a in raw.get("assignees", [])]

    # Filter comments to the lookback window (gh returns all historical comments)
    recent_comments = []
    for c in raw.get("comments", []):
        created_str = c.get("createdAt", "")
        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if created >= lookback_cutoff:
            recent_comments.append({
                "author": c.get("author", {}).get("login", "unknown"),
                "text": c.get("body", ""),
                "created": created_str,
            })

    return {
        "key": f"#{raw['number']}",
        "number": raw["number"],
        "summary": raw.get("title", ""),
        "status": "open",
        "assignee": assignees[0] if assignees else "unassigned",
        "assignees": assignees,
        "updated": updated_str,
        "stale_days": stale_days_count,
        "is_stale": is_stale,
        "is_blocked": is_blocked,
        "labels": label_names,
        "recent_comments": recent_comments,
        "url": raw.get("url", ""),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config(required=["GH_ORG", "GH_REPO", "GH_TOKEN"])
    workspace_dir = get_workspace_dir(config)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    lookback_hours = int(config.get("STANDUP_LOOKBACK_HOURS", "26"))
    stale_days = int(config.get("STANDUP_STALE_DAYS", "2"))
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    lookback_cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    team_filter = _parse_team_filter(config)

    print(
        f"[gather_gh] org={config['GH_ORG']} repo={config['GH_REPO']} "
        f"lookback={lookback_hours}h stale_after={stale_days}d",
        flush=True,
    )

    raw_issues = get_open_issues(config)
    print(f"[gather_gh] {len(raw_issues)} open issues fetched", flush=True)
    issues = [_normalise_issue(r, stale_cutoff, lookback_cutoff) for r in raw_issues]

    open_prs = get_open_prs(config)
    print(f"[gather_gh] {len(open_prs)} open PRs", flush=True)

    slack_messages = get_standup_messages(config, lookback_hours)

    signals = {
        "gathered_at": datetime.now(timezone.utc).isoformat(),
        "tracker": "github",
        "current_user": None,
        "sprint": {},
        "issues": issues,
        "open_prs": open_prs,
        "slack_messages": slack_messages,
        "config_summary": {
            "gh_org": config["GH_ORG"],
            "gh_repo": config["GH_REPO"],
            "lookback_hours": lookback_hours,
            "stale_days": stale_days,
            "slack_channel": config.get("SLACK_STANDUP_CHANNEL", "standup"),
            "team_filter": team_filter,
        },
    }

    (workspace_dir / "signals.json").write_text(json.dumps(signals, indent=2))
    (workspace_dir / "gather_result.json").write_text(json.dumps({
        "status": "DONE",
        "tracker": "github",
        "issue_count": len(issues),
        "pr_count": len(open_prs),
        "slack_message_count": len(slack_messages),
        "sprint": "",
    }, indent=2))

    print(
        f"[gather_gh] done: {len(issues)} issues, {len(open_prs)} PRs, "
        f"{len(slack_messages)} Slack msgs",
        flush=True,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
