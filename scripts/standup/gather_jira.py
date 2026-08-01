"""Gather standup signals from Jira, Bitbucket, and Slack.

Usage:
    python -m scripts.standup.gather_jira

Required env: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT
Optional env:
    BITBUCKET_WORKSPACE, BITBUCKET_REPO, BITBUCKET_USERNAME, BITBUCKET_TOKEN
    SLACK_BOT_TOKEN, SLACK_CHANNEL (default: standup)
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
from urllib.parse import urlparse, urlunparse

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


def _discover_jira_project(config: dict) -> str:
    """Return the first accessible project key, ordered by recent activity."""
    resp = requests.get(
        f"{_jira_base(config)}/rest/api/3/project/search",
        headers=_jira_headers(config),
        params={"maxResults": 1, "orderBy": "lastIssueUpdatedTime"},
        timeout=20,
    )
    if resp.ok:
        values = resp.json().get("values", [])
        if values:
            return values[0]["key"]
    raise RuntimeError(
        "JIRA_PROJECT not set and could not auto-discover a project — "
        "set JIRA_PROJECT env var or pass --jira-project"
    )


def _fetch_project_boards(config: dict) -> list[dict]:
    """Fetch all boards for JIRA_PROJECT (paginated, no cap)."""
    project = config.get("JIRA_PROJECT", "")
    if not project:
        return []
    base = _jira_base(config)
    headers = _jira_headers(config)
    boards: list[dict] = []
    start_at = 0
    while True:
        resp = requests.get(
            f"{base}/rest/agile/1.0/board",
            headers=headers,
            params={"projectKeyOrId": project, "maxResults": 50, "startAt": start_at},
            timeout=20,
        )
        if not resp.ok:
            print(f"[gather_jira] board list error {resp.status_code}: {resp.text[:200]}", file=sys.stderr, flush=True)
            break
        data = resp.json()
        batch = data.get("values", [])
        boards.extend(batch)
        if data.get("isLast", True) or not batch:
            break
        start_at += len(batch)
    return boards


def _get_my_sprint_ids(config: dict) -> set[int]:
    """Return sprint IDs of all open sprints that contain currentUser()'s issues."""
    resp = requests.get(
        f"{_jira_base(config)}/rest/api/3/search/jql",
        headers=_jira_headers(config),
        params={
            "jql": "assignee = currentUser() AND sprint in openSprints()",
            "maxResults": 100,
            "fields": "customfield_10010",  # sprint field (Jira Cloud standard)
        },
        timeout=30,
    )
    if not resp.ok:
        return set()
    sprint_ids: set[int] = set()
    for issue in resp.json().get("issues", []):
        for s in (issue.get("fields", {}).get("customfield_10010") or []):
            if isinstance(s, dict) and s.get("state") == "active":
                sprint_ids.add(s["id"])
    return sprint_ids


def get_active_sprints(config: dict) -> list[dict]:
    """Return active sprints relevant to the current user.

    Strategy:
    1. Find all sprint IDs that contain the current user's open issues.
    2. Find those sprints on all CRIBL scrum boards (to get board name + sprint detail).
    3. Fall back to the first board's sprint if user has no sprint-based issues.
    """
    my_sprint_ids = _get_my_sprint_ids(config)
    print(f"[gather_jira] current user is in {len(my_sprint_ids)} active sprint(s): {my_sprint_ids}", flush=True)

    all_boards = _fetch_project_boards(config)
    scrum_boards = [b for b in all_boards if b.get("type", "").lower() == "scrum"]

    base = _jira_base(config)
    headers = _jira_headers(config)

    # Collect all active sprints from all scrum boards, tag with board name
    all_active: dict[int, dict] = {}  # sprint_id → sprint (with _board_name list)
    for board in scrum_boards:
        board_id = board["id"]
        board_name = board.get("name", str(board_id))
        resp = requests.get(
            f"{base}/rest/agile/1.0/board/{board_id}/sprint",
            headers=headers,
            params={"state": "active", "maxResults": 10},
            timeout=20,
        )
        if not resp.ok:
            continue
        for sprint in resp.json().get("values", []):
            sid = sprint["id"]
            if sid not in all_active:
                sprint["_board_names"] = [board_name]
                sprint["_board_name"] = board_name
                all_active[sid] = sprint
            else:
                all_active[sid]["_board_names"].append(board_name)

    if not all_active:
        return []

    # If we know which sprints the user is in, return only those
    if my_sprint_ids:
        relevant = [all_active[sid] for sid in my_sprint_ids if sid in all_active]
        if relevant:
            names = [(s.get("name","?"), s.get("_board_names",[])) for s in relevant]
            print(f"[gather_jira] user's sprints: {names}", flush=True)
            return relevant

    # Fallback: return the first active sprint found (configured project board)
    first = next(iter(all_active.values()))
    print(f"[gather_jira] fallback: using first active sprint {first.get('name')} on {first.get('_board_name')}", flush=True)
    return [first]


def get_active_sprint(config: dict) -> dict | None:
    """Return the first active sprint found across all boards, or None."""
    sprints = get_active_sprints(config)
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
        print(f"[gather_jira] sprint issues error {resp.status_code}: {resp.text[:200]}", file=sys.stderr, flush=True)
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


def search_my_open_issues(config: dict) -> list[dict]:
    """Return all open issues assigned to currentUser() across all projects.

    This catches work that lives outside any sprint (backlog, kanban, cross-project).
    """
    jql = "assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
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
        print(f"[gather_jira] my-issues search error {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return []
    issues = resp.json().get("issues", [])
    print(f"[gather_jira] {len(issues)} open issues assigned to currentUser()", flush=True)
    return issues


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
        "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN",
    ])
    if not config.get("JIRA_PROJECT"):
        config["JIRA_PROJECT"] = _discover_jira_project(config)
        print(f"[gather_jira] auto-discovered project: {config['JIRA_PROJECT']}", flush=True)
    workspace_dir = get_workspace_dir(config)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    lookback_hours = int(config.get("STANDUP_LOOKBACK_HOURS", "26"))
    stale_days = int(config.get("STANDUP_STALE_DAYS", "2"))
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    raw_team = config.get("STANDUP_TEAM_MEMBERS", "").strip()
    # Treat Formicary's un-set template value as empty
    if raw_team in ("<no value>", "{{.StandupTeamMembers}}"):
        raw_team = ""
    team_filter = [m.strip() for m in raw_team.split(",") if m.strip()]

    print(f"[gather_jira] project={config['JIRA_PROJECT']} lookback={lookback_hours}h stale_after={stale_days}d", flush=True)

    me = get_current_jira_user(config)
    if not me:
        # Strip credentials from the URL before logging (https://user:token@host → https://host)
        raw_url = config["JIRA_BASE_URL"]
        parsed = urlparse(raw_url)
        safe_url = urlunparse(parsed._replace(netloc=parsed.hostname or ""))
        raise RuntimeError(
            f"Failed to authenticate with Jira at {safe_url} — "
            "check JIRA_EMAIL and JIRA_API_TOKEN"
        )
    print(f"[gather_jira] logged in as: {me.get('displayName', '?')}", flush=True)

    active_sprints = get_active_sprints(config)
    sprint_info: dict = {}
    all_sprint_infos: list[dict] = []
    seen_keys: set[str] = set()
    raw_issues: list[dict] = []
    if active_sprints:
        # Dedupe sprints by sprint id — multiple boards often share the same sprint
        seen_sprint_ids: set[int] = set()
        for sprint in active_sprints:
            board_names = sprint.get("_board_names") or [sprint.get("_board_name", "")]
            s_info = {
                "id": sprint["id"],
                "name": sprint.get("name", ""),
                "board": " · ".join(board_names),
                "state": sprint.get("state", ""),
                "start_date": sprint.get("startDate", ""),
                "end_date": sprint.get("endDate", ""),
            }
            all_sprint_infos.append(s_info)
            if sprint["id"] in seen_sprint_ids:
                continue
            seen_sprint_ids.add(sprint["id"])
            print(f"[gather_jira] sprint: {s_info['name']} (boards: {s_info['board']}) ends {s_info['end_date']}", flush=True)
            for issue in get_sprint_issues(config, sprint["id"]):
                if issue.get("key") not in seen_keys:
                    seen_keys.add(issue["key"])
                    raw_issues.append(issue)
        sprint_info = all_sprint_infos[0]
    else:
        print("[gather_jira] no active sprint — querying open project issues", flush=True)
        for issue in search_open_issues(config):
            if issue.get("key") not in seen_keys:
                seen_keys.add(issue["key"])
                raw_issues.append(issue)

    # Always merge issues assigned to the current user — they may be in a different
    # board/sprint or not in any sprint (backlog/kanban). This ensures the standup
    # includes the authenticated user's own work regardless of sprint structure.
    my_issues = search_my_open_issues(config)
    for issue in my_issues:
        if issue.get("key") not in seen_keys:
            seen_keys.add(issue["key"])
            raw_issues.append(issue)

    print(f"[gather_jira] {len(raw_issues)} issues total after merging my issues", flush=True)
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
        "all_sprints": all_sprint_infos,
        "issues": issues,
        "open_prs": open_prs,
        "slack_messages": slack_messages,
        "config_summary": {
            "jira_project": config["JIRA_PROJECT"],
            "jira_base_url": config["JIRA_BASE_URL"],
            "lookback_hours": lookback_hours,
            "stale_days": stale_days,
            "slack_channel": config.get("SLACK_CHANNEL", "standup"),
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
    from scripts.common.entrypoint import run_main
    run_main(main, "gather_result.json")
