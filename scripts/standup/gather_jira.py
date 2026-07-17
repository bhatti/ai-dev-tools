"""Gather standup signals from Jira, Bitbucket, and Slack.

Usage:
    python -m scripts.standup.gather_jira

Required env: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT
Optional env:
    BITBUCKET_WORKSPACE, BITBUCKET_REPO, BITBUCKET_USERNAME, BITBUCKET_TOKEN
    SLACK_BOT_TOKEN, SLACK_STANDUP_CHANNEL (default: standup)
    STANDUP_TEAM_MEMBERS  comma-separated Jira displayNames to scope brief;
                          default is all assignees with open sprint work
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
from base64 import b64encode
from datetime import datetime, timezone, timedelta

import requests

from scripts.common.config import load_config, get_workspace_dir
from scripts.standup.slack_client import get_standup_messages
from scripts.standup.bb_helpers import get_open_prs


# ---------------------------------------------------------------------------
# Jira REST helpers
# ---------------------------------------------------------------------------

def _jira_headers(config: dict) -> dict[str, str]:
    creds = b64encode(
        f"{config['JIRA_EMAIL']}:{config['JIRA_API_TOKEN']}".encode()
    ).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _jira_base(config: dict) -> str:
    return config["JIRA_BASE_URL"].rstrip("/")


def get_current_jira_user(config: dict) -> dict | None:
    resp = requests.get(
        f"{_jira_base(config)}/rest/api/3/myself",
        headers=_jira_headers(config),
        timeout=20,
    )
    return resp.json() if resp.ok else None


def get_active_sprint(config: dict) -> dict | None:
    """Return the first active sprint for the project's board, or None."""
    board_resp = requests.get(
        f"{_jira_base(config)}/rest/agile/1.0/board",
        headers=_jira_headers(config),
        params={"projectKeyOrId": config["JIRA_PROJECT"], "maxResults": 1},
        timeout=20,
    )
    if not board_resp.ok:
        return None
    boards = board_resp.json().get("values", [])
    if not boards:
        return None
    board_id = boards[0]["id"]

    sprint_resp = requests.get(
        f"{_jira_base(config)}/rest/agile/1.0/board/{board_id}/sprint",
        headers=_jira_headers(config),
        params={"state": "active", "maxResults": 1},
        timeout=20,
    )
    if not sprint_resp.ok:
        return None
    sprints = sprint_resp.json().get("values", [])
    return sprints[0] if sprints else None


def get_sprint_issues(config: dict, sprint_id: int) -> list[dict]:
    """Fetch all issues in a sprint, including embedded comments."""
    resp = requests.get(
        f"{_jira_base(config)}/rest/agile/1.0/sprint/{sprint_id}/issue",
        headers=_jira_headers(config),
        params={
            "fields": "summary,status,assignee,updated,labels,priority,comment",
            "maxResults": 200,
        },
        timeout=30,
    )
    if not resp.ok:
        return []
    return resp.json().get("issues", [])


def search_open_issues(config: dict) -> list[dict]:
    """JQL fallback when there is no active sprint. Requests all needed fields."""
    project = config["JIRA_PROJECT"]
    jql = (
        f'project = "{project}" AND statusCategory != Done ORDER BY updated DESC'
    )
    resp = requests.get(
        f"{_jira_base(config)}/rest/api/3/search/jql",
        headers=_jira_headers(config),
        params={
            "jql": jql,
            "maxResults": 100,
            "fields": "summary,status,assignee,updated,labels,priority,comment",
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"[gather_jira] search error {resp.status_code}: {resp.text}", file=sys.stderr)
        return []
    return resp.json().get("issues", [])


def _extract_embedded_comments(fields: dict, lookback_hours: int) -> list[dict]:
    """Extract recent comments from the 'comment' field already embedded in the issue."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    recent = []
    for c in fields.get("comment", {}).get("comments", []):
        created_str = c.get("created", "")
        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if created >= cutoff:
            body = c.get("body", {})
            text = _adf_text(body) if isinstance(body, dict) else str(body)
            author = c.get("author", {}).get("displayName", "unknown")
            recent.append({"author": author, "text": text, "created": created_str})
    return recent


def _adf_text(node: dict) -> str:
    """Recursively extract plain text from Atlassian Document Format."""
    if node.get("type") == "text":
        return node.get("text", "")
    return " ".join(
        _adf_text(child) for child in node.get("content", [])
    ).strip()


# ---------------------------------------------------------------------------
# Issue normalisation
# ---------------------------------------------------------------------------

def _normalise_issue(raw: dict, stale_cutoff: datetime, config: dict, lookback_hours: int) -> dict:
    key = raw.get("key", "")
    fields = raw.get("fields", {})
    updated_str = fields.get("updated", "")
    try:
        updated = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
        stale_days_count = (datetime.now(timezone.utc) - updated).days
        is_stale = updated < stale_cutoff
    except (ValueError, AttributeError):
        stale_days_count = 0
        is_stale = False

    assignee = fields.get("assignee") or {}
    status = fields.get("status", {}).get("name", "unknown")
    labels = fields.get("labels", [])
    # substring match so "blocked-by-dep", "is-blocked" etc. are all caught
    is_blocked = (
        any("blocked" in lbl.lower() for lbl in labels)
        or "blocked" in status.lower()
    )

    # Use the comments already embedded in the response — avoids N+1 HTTP calls
    recent_comments = _extract_embedded_comments(fields, lookback_hours)

    return {
        "key": key,
        "summary": fields.get("summary", ""),
        "status": status,
        "assignee": assignee.get("displayName", "unassigned"),
        "assignee_account_id": assignee.get("accountId", ""),
        "updated": updated_str,
        "stale_days": stale_days_count,
        "is_stale": is_stale,
        "is_blocked": is_blocked,
        "labels": labels,
        "priority": fields.get("priority", {}).get("name", ""),
        "recent_comments": recent_comments,
        "url": f"{_jira_base(config)}/browse/{key}",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config(required=[
        "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECT",
    ])
    workspace_dir = get_workspace_dir(config)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    lookback_hours = int(config.get("STANDUP_LOOKBACK_HOURS", "26"))
    stale_days = int(config.get("STANDUP_STALE_DAYS", "2"))
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    team_filter = [
        m.strip() for m in config.get("STANDUP_TEAM_MEMBERS", "").split(",") if m.strip()
    ]

    print(f"[gather_jira] project={config['JIRA_PROJECT']} lookback={lookback_hours}h stale_after={stale_days}d", flush=True)

    me = get_current_jira_user(config)
    print(f"[gather_jira] logged in as: {me.get('displayName', '?') if me else 'unknown'}", flush=True)

    sprint = get_active_sprint(config)
    sprint_info: dict = {}
    if sprint:
        sprint_info = {
            "id": sprint["id"],
            "name": sprint.get("name", ""),
            "state": sprint.get("state", ""),
            "start_date": sprint.get("startDate", ""),
            "end_date": sprint.get("endDate", ""),
        }
        print(f"[gather_jira] sprint: {sprint_info['name']} ends {sprint_info['end_date']}", flush=True)
        raw_issues = get_sprint_issues(config, sprint["id"])
    else:
        print("[gather_jira] no active sprint — querying open project issues", flush=True)
        raw_issues = search_open_issues(config)

    print(f"[gather_jira] {len(raw_issues)} issues fetched", flush=True)
    issues = [_normalise_issue(r, stale_cutoff, config, lookback_hours) for r in raw_issues]

    if team_filter:
        issues = [i for i in issues if i["assignee"] in team_filter]
        print(f"[gather_jira] filtered to {len(issues)} issues for team {team_filter}", flush=True)

    open_prs = get_open_prs(config)
    print(f"[gather_jira] {len(open_prs)} open Bitbucket PRs", flush=True)

    slack_messages = get_standup_messages(config, lookback_hours)

    signals = {
        "gathered_at": datetime.now(timezone.utc).isoformat(),
        "tracker": "jira",
        "current_user": me,
        "sprint": sprint_info,
        "issues": issues,
        "open_prs": open_prs,
        "slack_messages": slack_messages,
        "config_summary": {
            "jira_project": config["JIRA_PROJECT"],
            "jira_base_url": config["JIRA_BASE_URL"],
            "lookback_hours": lookback_hours,
            "stale_days": stale_days,
            "slack_channel": config.get("SLACK_STANDUP_CHANNEL", "standup"),
            "team_filter": team_filter,
        },
    }

    (workspace_dir / "signals.json").write_text(json.dumps(signals, indent=2))
    (workspace_dir / "gather_result.json").write_text(json.dumps({
        "status": "DONE",
        "tracker": "jira",
        "issue_count": len(issues),
        "pr_count": len(open_prs),
        "slack_message_count": len(slack_messages),
        "sprint": sprint_info.get("name", ""),
    }, indent=2))

    print(
        f"[gather_jira] done: {len(issues)} issues, {len(open_prs)} PRs, "
        f"{len(slack_messages)} Slack msgs",
        flush=True,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
