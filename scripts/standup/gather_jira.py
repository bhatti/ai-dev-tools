"""Gather standup signals from Jira, Bitbucket, and Slack.

Usage:
    python -m scripts.standup.gather_jira

Required env: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT
Optional env:
    JIRA_BOARDS           comma-separated board IDs (e.g. "4161") — skips board scan when set.
                          Find your board ID in the Jira URL: /boards/<ID>.
                          Leave unset to auto-discover boards where you have active work.
    STANDUP_TEAM_MEMBERS  comma-separated Jira displayNames; default = all sprint assignees
    BITBUCKET_WORKSPACE, BITBUCKET_REPO, BITBUCKET_USERNAME, BITBUCKET_TOKEN
    SLACK_BOT_TOKEN, SLACK_CHANNEL (default: standup)
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


def _get_active_sprint_for_board(config: dict, board_id: int) -> dict | None:
    """Return the active sprint for a board, or None."""
    resp = requests.get(
        f"{_jira_base(config)}/rest/agile/1.0/board/{board_id}/sprint",
        headers=_jira_headers(config),
        params={"state": "active", "maxResults": 1},
        timeout=20,
    )
    if not resp.ok:
        return None
    sprints = resp.json().get("values", [])
    return sprints[0] if sprints else None


def _parse_board_ids(config: dict) -> set[int]:
    """Parse JIRA_BOARDS env var — comma-separated numeric board IDs.

    When set, the sprint-board scan is skipped entirely (faster, cheaper).
    Find your board ID in the Jira URL: /jira/software/c/projects/PROJ/boards/<ID>
    """
    raw = config.get("JIRA_BOARDS", "").strip()
    if raw in ("<no value>", "{{.JiraBoards}}", ""):
        return set()
    return {int(p.strip()) for p in raw.split(",") if p.strip().isdigit()}


def _fetch_board_metadata(config: dict, board_id: int) -> dict:
    """Fetch board name and type for an explicit board ID."""
    resp = requests.get(
        f"{_jira_base(config)}/rest/agile/1.0/board/{board_id}",
        headers=_jira_headers(config),
        timeout=20,
    )
    if resp.ok:
        return resp.json()
    return {"id": board_id, "name": str(board_id), "type": "scrum"}


def get_active_sprints(config: dict, me: dict | None = None, team_filter: list[str] | None = None) -> list[dict]:
    """Return active sprints on boards where the current user or team has work.

    When JIRA_BOARDS is set, uses those board IDs directly — no project config needed,
    no board scan.  This is the fast path and works even without JIRA_PROJECT set.

    When JIRA_BOARDS is empty, auto-discovers relevant boards by scanning all scrum
    boards for JIRA_PROJECT, fetching each active sprint's issues, and checking assignees.

    Each returned sprint is tagged with _board_id, _board_name, _board_names.
    """
    board_id_override = _parse_board_ids(config)

    if board_id_override:
        # Fast path: explicit board IDs — bypass _fetch_project_boards entirely.
        # This works regardless of JIRA_PROJECT setting (board ID is authoritative).
        scrum_boards = [_fetch_board_metadata(config, bid) for bid in sorted(board_id_override)]
        print(f"[gather_jira] using explicit JIRA_BOARDS={sorted(board_id_override)} → {len(scrum_boards)} board(s)", flush=True)
    else:
        all_boards = _fetch_project_boards(config)
        scrum_boards = [b for b in all_boards if b.get("type", "").lower() == "scrum"]
        print(f"[gather_jira] scanning {len(scrum_boards)} scrum board(s) for team work...", flush=True)

    me_account_id = me.get("accountId", "") if me else ""
    me_display_name = me.get("displayName", "") if me else ""

    relevant_account_ids: set[str] = {me_account_id} if me_account_id else set()
    relevant_display_names: set[str] = set(team_filter) if team_filter else set()
    if me_display_name:
        relevant_display_names.add(me_display_name)

    base = _jira_base(config)
    headers = _jira_headers(config)
    all_active: dict[int, dict] = {}  # sprint_id → sprint

    for board in scrum_boards:
        board_id = board["id"]
        board_name = board.get("name", str(board_id))

        sprint = _get_active_sprint_for_board(config, board_id)
        if not sprint:
            continue
        sid = sprint["id"]

        # When board IDs are explicit we trust them — skip the membership scan
        if not board_id_override:
            board_relevant = False
            start = 0
            while not board_relevant:
                resp = requests.get(
                    f"{base}/rest/agile/1.0/sprint/{sid}/issue",
                    headers=headers,
                    params={"fields": "assignee", "maxResults": 200, "startAt": start},
                    timeout=20,
                )
                if not resp.ok:
                    break
                data = resp.json()
                for i in data.get("issues", []):
                    a = i.get("fields", {}).get("assignee") or {}
                    if (a.get("accountId", "") in relevant_account_ids
                            or a.get("displayName", "") in relevant_display_names):
                        board_relevant = True
                        break
                fetched = len(data.get("issues", []))
                total = data.get("total", 0)
                start += fetched
                if board_relevant or fetched == 0 or start >= total:
                    break
            if not board_relevant:
                continue

        print(f"[gather_jira] relevant board: {board_name} (id={board_id}) sprint={sprint.get('name', sid)}", flush=True)

        if sid not in all_active:
            sprint["_board_id"] = board_id
            sprint["_board_names"] = [board_name]
            sprint["_board_name"] = board_name
            all_active[sid] = sprint
        else:
            all_active[sid]["_board_names"].append(board_name)

    if not all_active:
        return []

    sprints = list(all_active.values())
    print(f"[gather_jira] {len(sprints)} relevant active sprint(s)", flush=True)
    return sprints


def get_active_sprint(config: dict, me: dict | None = None) -> dict | None:
    """Return the first active sprint found across all boards, or None."""
    sprints = get_active_sprints(config, me=me)
    return sprints[0] if sprints else None


def get_sprint_issues(
    config: dict,
    sprint_id: int | None = None,
    team_filter: list[str] | None = None,
    board_id: int | None = None,
) -> list[dict]:
    """Fetch sprint issues for a board sprint or project-wide open sprints.

    When board_id is provided, issues are fetched via the agile board endpoint so
    only issues on that board are returned (correct scoping).  When sprint_id is
    also provided it is used as an additional filter.  When neither is set the
    fallback is project-wide openSprints() JQL.

    Each returned raw issue is tagged with '_board_id' when board_id is known.
    """
    project = config.get("JIRA_PROJECT", "")
    base = _jira_base(config)
    headers = _jira_headers(config)
    field_list = "summary,status,assignee,updated,labels,priority,comment"

    if board_id is not None:
        # Board-scoped fetch: returns only issues currently on that board
        endpoint = f"{base}/rest/agile/1.0/board/{board_id}/issue"
        if sprint_id is not None:
            endpoint = f"{base}/rest/agile/1.0/sprint/{sprint_id}/issue"
        params: dict = {"fields": field_list, "maxResults": 200}
        resp = requests.get(endpoint, headers=headers, params=params, timeout=30)
        if not resp.ok:
            print(f"[gather_jira] board {board_id} issues error {resp.status_code}: {resp.text[:200]}", file=sys.stderr, flush=True)
            return []
        issues = resp.json().get("issues", [])
        for i in issues:
            i["_board_id"] = board_id
        return issues

    # Fallback: project-wide JQL
    if sprint_id is not None:
        sprint_clause = f"sprint = {sprint_id}"
    else:
        sprint_clause = f'project = "{project}" AND sprint in openSprints()'

    if team_filter:
        members_jql = ", ".join(f'"{m}"' for m in team_filter)
        jql = f"{sprint_clause} AND assignee in ({members_jql})"
    else:
        jql = f"{sprint_clause} AND assignee is not EMPTY"

    resp = requests.get(
        f"{base}/rest/api/3/search/jql",
        headers=headers,
        params={"jql": jql, "fields": field_list, "maxResults": 200},
        timeout=30,
    )
    if not resp.ok:
        print(f"[gather_jira] sprint issues JQL error {resp.status_code}: {resp.text[:200]}", file=sys.stderr, flush=True)
        return []
    return resp.json().get("issues", [])


def search_open_issues(config: dict, team_filter: list[str] | None = None) -> list[dict]:
    """JQL fallback when there is no active sprint. Scoped to team or all assignees."""
    project = config.get("JIRA_PROJECT", "")
    if not project:
        print("[gather_jira] search_open_issues: JIRA_PROJECT not set — cannot run JQL fallback", file=sys.stderr)
        return []
    if team_filter:
        members_jql = ", ".join(f'"{m}"' for m in team_filter)
        assignee_clause = f"AND assignee in ({members_jql})"
    else:
        assignee_clause = "AND assignee is not EMPTY"
    jql = (
        f'project = "{project}" {assignee_clause} AND statusCategory != Done ORDER BY updated DESC'
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
    """Return open issues assigned to currentUser() within the configured project.

    Scoped to JIRA_PROJECT to avoid pulling unrelated tickets from other boards.
    """
    project = config.get("JIRA_PROJECT", "")
    project_clause = f'project = "{project}" AND ' if project else ""
    jql = f"{project_clause}assignee = currentUser() AND statusCategory != Done ORDER BY updated DESC"
    resp = requests.get(
        f"{_jira_base(config)}/rest/api/3/search/jql",
        headers=_jira_headers(config),
        params={
            "jql": jql,
            "maxResults": 50,
            "fields": "summary,status,assignee,updated,labels,priority,comment",
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"[gather_jira] my-issues search error {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return []
    issues = resp.json().get("issues", [])
    print(f"[gather_jira] {len(issues)} open issues assigned to currentUser() in {project or 'all projects'}", flush=True)
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
    is_blocked = (
        any("blocked" in lbl.lower() for lbl in labels)
        or "blocked" in status.lower()
    )

    recent_comments = _extract_embedded_comments(fields, lookback_hours)

    return {
        "key": key,
        "id": str(raw.get("id", "")),
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
        "board_id": raw.get("_board_id"),   # None when not board-scoped
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    config = load_config(required=[
        "JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN",
    ])
    board_ids = _parse_board_ids(config)
    if not config.get("JIRA_PROJECT"):
        if board_ids:
            # Board IDs are explicit — project lookup is not needed.
            print("[gather_jira] JIRA_BOARDS set — skipping JIRA_PROJECT auto-discovery", flush=True)
        else:
            config["JIRA_PROJECT"] = _discover_jira_project(config)
            print(f"[gather_jira] auto-discovered project: {config['JIRA_PROJECT']}", flush=True)
    workspace_dir = get_workspace_dir(config)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    lookback_hours = int(config.get("STANDUP_LOOKBACK_HOURS", "26"))
    stale_days = int(config.get("STANDUP_STALE_DAYS", "2"))
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    raw_team = config.get("STANDUP_TEAM_MEMBERS", "").strip()
    if raw_team in ("<no value>", "{{.StandupTeamMembers}}"):
        raw_team = ""
    team_filter = [m.strip() for m in raw_team.split(",") if m.strip()]

    print(
        f"[gather_jira] project={config.get('JIRA_PROJECT', '(from board)')} "
        f"lookback={lookback_hours}h stale_after={stale_days}d",
        flush=True,
    )

    me = get_current_jira_user(config)
    if not me:
        raw_url = config["JIRA_BASE_URL"]
        parsed = urlparse(raw_url)
        safe_url = urlunparse(parsed._replace(netloc=parsed.hostname or ""))
        raise RuntimeError(
            f"Failed to authenticate with Jira at {safe_url} — "
            "check JIRA_EMAIL and JIRA_API_TOKEN"
        )
    print(f"[gather_jira] logged in as: {me.get('displayName', '?')}", flush=True)

    # Auto-discover boards where the current user or configured team has active work.
    # No manual JIRA_BOARDS config required.
    active_sprints = get_active_sprints(config, me=me, team_filter=team_filter or None)
    sprint_info: dict = {}
    all_sprint_infos: list[dict] = []
    # board_id → sprint_info (for signals)
    board_sprint_map: dict[int, dict] = {}
    seen_keys: set[str] = set()
    raw_issues: list[dict] = []

    if active_sprints:
        seen_sprint_ids: set[int] = set()
        for sprint in active_sprints:
            board_id = sprint.get("_board_id")
            board_names = sprint.get("_board_names") or [sprint.get("_board_name", "")]
            s_info = {
                "id": sprint["id"],
                "board_id": board_id,
                "name": sprint.get("name", ""),
                "board": " · ".join(board_names),
                "state": sprint.get("state", ""),
                "start_date": sprint.get("startDate", ""),
                "end_date": sprint.get("endDate", ""),
            }
            if sprint["id"] not in seen_sprint_ids:
                seen_sprint_ids.add(sprint["id"])
                all_sprint_infos.append(s_info)
                if board_id:
                    board_sprint_map[board_id] = s_info
                print(
                    f"[gather_jira] sprint: {s_info['name']} board_id={board_id} "
                    f"(boards: {s_info['board']}) ends {s_info['end_date']}",
                    flush=True,
                )
        sprint_info = all_sprint_infos[0]

        # Fetch issues per board so each issue carries its board_id.
        # When board_id_filter is set we already have the target boards; otherwise
        # fall back to project-wide openSprints() JQL (no board attribution).
        all_boards_with_sprints = {s.get("_board_id") for s in active_sprints if s.get("_board_id")}
        if all_boards_with_sprints:
            for bid in all_boards_with_sprints:
                # Use the sprint id if we know it (one active sprint per board is typical)
                sprint_for_board = next(
                    (s for s in active_sprints if s.get("_board_id") == bid), None
                )
                sid = sprint_for_board["id"] if sprint_for_board else None
                print(f"[gather_jira] fetching issues for board {bid} sprint {sid}...", flush=True)
                for issue in get_sprint_issues(
                    config, sprint_id=sid, team_filter=team_filter or None, board_id=bid
                ):
                    if issue.get("key") not in seen_keys:
                        seen_keys.add(issue["key"])
                        raw_issues.append(issue)
        else:
            print("[gather_jira] fetching all open-sprint issues (project-wide fallback)...", flush=True)
            for issue in get_sprint_issues(config, sprint_id=None, team_filter=team_filter or None):
                if issue.get("key") not in seen_keys:
                    seen_keys.add(issue["key"])
                    raw_issues.append(issue)
    else:
        print("[gather_jira] no active sprint — querying open project issues", flush=True)
        for issue in search_open_issues(config, team_filter or None):
            if issue.get("key") not in seen_keys:
                seen_keys.add(issue["key"])
                raw_issues.append(issue)

    print(f"[gather_jira] {len(raw_issues)} issues total (raw)", flush=True)
    issues = [_normalise_issue(r, stale_cutoff, config, lookback_hours) for r in raw_issues]

    if team_filter:
        issues = [i for i in issues if i["assignee"] in team_filter]
        print(f"[gather_jira] filtered to {len(issues)} issues for team {team_filter}", flush=True)

    # Derive team members from the actual assignees on board issues (not current_user)
    team_members = sorted({i["assignee"] for i in issues if i["assignee"] != "unassigned"})
    print(f"[gather_jira] team members from issues: {team_members}", flush=True)

    open_prs = get_open_prs(config)
    # Filter PRs to team members derived from board issues
    if team_members:
        filtered_prs = [
            pr for pr in open_prs
            if pr.get("author", "") in team_members
            or any(r in team_members for r in pr.get("reviewers", []))
        ]
        print(
            f"[gather_jira] {len(open_prs)} open Bitbucket PRs → "
            f"{len(filtered_prs)} after team filter",
            flush=True,
        )
        open_prs = filtered_prs
    else:
        print(f"[gather_jira] {len(open_prs)} open Bitbucket PRs (no team filter)", flush=True)

    slack_messages = get_standup_messages(config, lookback_hours)

    signals = {
        "gathered_at": datetime.now(timezone.utc).isoformat(),
        "tracker": "jira",
        # Omit current_user from signals — it is the API auth identity only and
        # must not anchor the per-person analysis in synthesize.
        "team_members": team_members,
        "sprint": sprint_info,
        "all_sprints": all_sprint_infos,
        "board_sprint_map": {str(k): v for k, v in board_sprint_map.items()},
        "issues": issues,
        "open_prs": open_prs,
        "slack_messages": slack_messages,
        "config_summary": {
            "jira_project": config.get("JIRA_PROJECT", ""),
            "jira_base_url": config["JIRA_BASE_URL"],
            "lookback_hours": lookback_hours,
            "stale_days": stale_days,
            "slack_channel": config.get("SLACK_CHANNEL", ""),
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
        "team_member_count": len(team_members),
    }, indent=2))

    print(
        f"[gather_jira] done: {len(issues)} issues, {len(open_prs)} PRs, "
        f"{len(slack_messages)} Slack msgs, {len(team_members)} team members",
        flush=True,
    )
    print(f"::add-task-context SELECTED_TRACKER::jira")
    print(f"::add-task-context ISSUE_COUNT::{len(issues)}")
    print(f"::add-task-context PR_COUNT::{len(open_prs)}")
    print(f"::add-task-context SLACK_MESSAGE_COUNT::{len(slack_messages)}")
    print(f"::add-task-context TEAM_MEMBER_COUNT::{len(team_members)}")
    if sprint_info:
        print(f"::add-task-context SPRINT_NAME::{sprint_info.get('name', '')}")
    sys.exit(0)


if __name__ == "__main__":
    from scripts.common.entrypoint import run_main
    run_main(main, "gather_result.json")
