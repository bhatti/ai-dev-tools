"""Jira REST API client using basic auth (email + API token).

All operations use the Atlassian Cloud REST API v3.
Auth: base64(email:api_token) in Authorization header.

Required env (passed via config dict):
    JIRA_BASE_URL  — e.g. https://myorg.atlassian.net
    JIRA_EMAIL     — Atlassian account email
    JIRA_API_TOKEN — Jira API token
"""

import json
import re
import sys
from base64 import b64encode

import requests

_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")
_JIRA_URL_RE = re.compile(r"https?://[^/]+/browse/([A-Z][A-Z0-9_]+-\d+)")


def extract_jira_keys(text: str) -> list[str]:
    """Extract all Jira issue keys from free-form text (prose, comma lists, browse URLs).

    Uses finditer so keys embedded anywhere in the text are found, e.g.
    "give tldr for PROJ-123" → ["PROJ-123"].
    """
    found: list[str] = []
    seen: set[str] = set()
    for m in _JIRA_URL_RE.finditer(text):
        k = m.group(1)
        if k not in seen:
            found.append(k)
            seen.add(k)
    for m in _JIRA_KEY_RE.finditer(text):
        k = m.group(1)
        if k not in seen:
            found.append(k)
            seen.add(k)
    return found


def resolve_jira_issues(
    config: dict,
    query: str | None = None,
    issues_arg: str | None = None,
    issue_type: str | None = None,
    max_results: int = 20,
    build_jql_fn=None,
) -> list[dict]:
    """Resolve Jira issues from a query or explicit key list.

    Resolution order:
    1. If ``issues_arg`` is given, extract issue keys and fetch each directly.
    2. If ``query`` contains embedded issue keys, fetch them directly.
    3. Otherwise call ``build_jql_fn(config, query, issue_type)`` and search.

    ``build_jql_fn`` is injected to avoid a circular import with query_issues.py.
    """
    if issues_arg:
        keys = extract_jira_keys(issues_arg)
        if keys:
            return [i for i in (get_issue(config, k) for k in keys) if i]

    if query:
        inline_keys = extract_jira_keys(query)
        if inline_keys:
            print(f"[jira] found issue key(s) in text: {inline_keys}", flush=True)
            return [i for i in (get_issue(config, k) for k in inline_keys) if i]

    if build_jql_fn and (query or issue_type):
        jql = build_jql_fn(config, query or "", issue_type)
        print(f"[jira] JQL: {jql}", flush=True)
        return search_issues(config, jql, max_results=max_results)

    return []


def extract_adf_text(node: "dict | str | None", depth: int = 0) -> str:
    """Recursively extract plain text from Atlassian Document Format (ADF).

    Consolidates _extract_plain_text (query_issues) and _extract_text_from_doc
    (analyze_issues) into a single canonical implementation.
    """
    if node is None or depth > 10:
        return ""
    if isinstance(node, str):
        return node.strip()
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text", "")
    parts = [extract_adf_text(child, depth + 1) for child in node.get("content", [])]
    return " ".join(p for p in parts if p)


def _auth_headers(config: dict) -> dict[str, str]:
    email = config["JIRA_EMAIL"]
    token = config["JIRA_API_TOKEN"]
    creds = b64encode(f"{email}:{token}".encode()).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _base(config: dict) -> str:
    return config["JIRA_BASE_URL"].rstrip("/")


_field_id_cache: dict[str, str] = {}
_user_team_cache: dict[str, str] = {}


def resolve_field_id(config: dict, field_name: str) -> str | None:
    """Resolve a Jira field name to its customfield ID. Strips whitespace. Cached."""
    norm = field_name.lower().replace(" ", "")
    if norm in _field_id_cache:
        return _field_id_cache[norm]
    try:
        resp = requests.get(
            f"{_base(config)}/rest/api/3/field",
            headers=_auth_headers(config),
            timeout=15,
        )
        if resp.ok:
            for f in resp.json():
                if f.get("name", "").lower().replace(" ", "") == norm:
                    _field_id_cache[norm] = f["id"]
                    return f["id"]
    except Exception:
        pass
    return None


def resolve_current_user_team(config: dict) -> str | None:
    """Auto-detect the current Jira user's team from their recent issue assignments.

    Uses `currentUser()` in JQL (no separate /myself round-trip needed) to find
    the team custom field value on a recently assigned issue.
    Cached per JIRA_EMAIL so repeated calls are cheap.
    """
    cache_key = (config.get("JIRA_EMAIL") or "").lower()
    if cache_key and cache_key in _user_team_cache:
        return _user_team_cache[cache_key]

    team_field_name = config.get("JIRA_TEAM_FIELD", "Eng Scrum Team")
    field_id = resolve_field_id(config, team_field_name)
    if not field_id:
        print(f"[jira-api] team field '{team_field_name}' not found — cannot auto-detect team", file=sys.stderr)
        return None

    try:
        resp = requests.post(
            f"{_base(config)}/rest/api/3/search/jql",
            headers=_auth_headers(config),
            json={
                "jql": "assignee = currentUser() AND updated >= -30d ORDER BY updated DESC",
                "maxResults": 20,
                "fields": [field_id],
            },
            timeout=15,
        )
        if not resp.ok:
            return None
        for issue in resp.json().get("issues", []):
            team_val = issue.get("fields", {}).get(field_id)
            if team_val:
                team_str = (
                    team_val.get("value") or team_val.get("name")
                    if isinstance(team_val, dict) else str(team_val)
                )
                if team_str:
                    print(f"[jira-api] auto-detected team: '{team_str}'", flush=True)
                    if cache_key:
                        _user_team_cache[cache_key] = team_str
                    return team_str
    except Exception as e:
        print(f"[jira-api] team auto-detect error: {e}", file=sys.stderr)
    return None


def fetch_board_issue_keys(
    config: dict, board_id: "str | int", max_results: int = 500, days_back: int = 90
) -> set[str]:
    """Return Jira issue keys for recent board sprints (including Done issues).

    Fetches active + recently closed sprints and collects all issue keys from
    each sprint.  Sprint-scoped fetch includes Done issues (unlike the board/issue
    endpoint which excludes Done by default).  This matches how standup's
    'open prs' flow finds sprint-linked PRs.

    Falls back to the board/issue endpoint for Kanban boards (no sprints).
    """
    from datetime import datetime, timezone, timedelta

    base = _base(config)
    headers = _auth_headers(config)
    keys: set[str] = set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    # --- Step 1: get the most recent closed + active sprints for this board ---
    # Jira returns sprints oldest-first, so we must jump to the last page to
    # get recent ones.  First call gets total count, second call fetches the last 50.
    try:
        count_resp = requests.get(
            f"{base}/rest/agile/1.0/board/{board_id}/sprint",
            headers=headers,
            params={"state": "active,closed", "maxResults": 1},
            timeout=30,
        )
        if not count_resp.ok:
            print(f"[jira-api] board/{board_id}/sprint error {count_resp.status_code}", file=sys.stderr)
            return _fetch_board_issues_direct(config, board_id, max_results)
        total_sprints = count_resp.json().get("total", 0)
        start_at = max(0, total_sprints - 50)  # last 50 sprints (most recent)

        sresp = requests.get(
            f"{base}/rest/agile/1.0/board/{board_id}/sprint",
            headers=headers,
            params={"state": "active,closed", "maxResults": 50, "startAt": start_at},
            timeout=30,
        )
    except Exception as e:
        print(f"[jira-api] board/{board_id}/sprint fetch error: {e}", file=sys.stderr)
        return _fetch_board_issues_direct(config, board_id, max_results)

    if not sresp.ok:
        print(f"[jira-api] board/{board_id}/sprint error {sresp.status_code}", file=sys.stderr)
        return _fetch_board_issues_direct(config, board_id, max_results)

    sprints = sresp.json().get("values", [])
    if not sprints:
        # Kanban or API unavailable — fall back to direct board/issue endpoint
        return _fetch_board_issues_direct(config, board_id, max_results)

    # Filter to sprints that are active or ended within days_back
    recent: list[dict] = []
    for sp in sprints:
        if sp.get("state") == "active":
            recent.append(sp)
            continue
        end_str = sp.get("completeDate") or sp.get("endDate", "")
        try:
            end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            if end_dt >= cutoff:
                recent.append(sp)
        except (ValueError, TypeError):
            recent.append(sp)  # include on parse failure

    if not recent:
        recent = sprints  # all sprints fetched are old, use them anyway

    # --- Step 2: fetch issues for each recent sprint ---
    for sp in recent:
        sprint_id = sp.get("id")
        if not sprint_id:
            continue
        start = 0
        while len(keys) < max_results:
            try:
                iresp = requests.get(
                    f"{base}/rest/agile/1.0/sprint/{sprint_id}/issue",
                    headers=headers,
                    params={"startAt": start, "maxResults": 100, "fields": "summary"},
                    timeout=30,
                )
            except Exception as e:
                print(f"[jira-api] sprint/{sprint_id}/issue error: {e}", file=sys.stderr)
                break
            if not iresp.ok:
                break
            data = iresp.json()
            for issue in data.get("issues", []):
                keys.add(issue["key"])
            fetched = len(data.get("issues", []))
            start += fetched
            if fetched == 0 or start >= data.get("total", 0):
                break

    if not keys:
        # Sprint API returned nothing — fall back to board/issue endpoint
        keys = _fetch_board_issues_direct(config, board_id, max_results)

    return keys


def _fetch_board_issues_direct(
    config: dict, board_id: "str | int", max_results: int
) -> set[str]:
    """Fallback: board/issue endpoint (excludes Done by default, used for Kanban)."""
    keys: set[str] = set()
    start = 0
    while start < max_results:
        resp = requests.get(
            f"{_base(config)}/rest/agile/1.0/board/{board_id}/issue",
            headers=_auth_headers(config),
            params={"startAt": start, "maxResults": 50, "fields": "summary"},
            timeout=30,
        )
        if not resp.ok:
            print(f"[jira-api] board/{board_id}/issue error {resp.status_code}", file=sys.stderr)
            break
        data = resp.json()
        for issue in data.get("issues", []):
            keys.add(issue["key"])
        fetched = len(data.get("issues", []))
        start += fetched
        if fetched == 0 or start >= data.get("total", 0):
            break
    return keys


def search_issues(
    config: dict,
    jql: str,
    max_results: int = 20,
    fields: "list[str] | None" = None,
) -> list[dict]:
    """Search Jira issues by JQL. Returns list of issue dicts."""
    default_fields = ["summary", "description", "labels", "status", "assignee", "priority", "issuetype", "created"]
    resp = requests.post(
        f"{_base(config)}/rest/api/3/search/jql",
        headers=_auth_headers(config),
        json={"jql": jql, "maxResults": max_results, "fields": fields or default_fields},
        timeout=30,
    )
    if not resp.ok:
        print(f"Jira search error {resp.status_code}: {resp.text}", file=sys.stderr)
        return []
    return resp.json().get("issues", [])


def get_issue(config: dict, issue_key: str) -> dict | None:
    """Fetch a single Jira issue by key."""
    url = f"{_base(config)}/rest/api/3/issue/{issue_key}"
    resp = requests.get(url, headers=_auth_headers(config), timeout=30)
    if not resp.ok:
        return None
    return resp.json()


def get_issue_labels(config: dict, issue_key: str) -> list[str]:
    """Return current labels on a Jira issue."""
    issue = get_issue(config, issue_key)
    if not issue:
        return []
    return issue.get("fields", {}).get("labels", [])


def set_issue_labels(config: dict, issue_key: str, labels: list[str]) -> bool:
    """Overwrite all labels on a Jira issue."""
    url = f"{_base(config)}/rest/api/3/issue/{issue_key}"
    resp = requests.put(
        url,
        headers=_auth_headers(config),
        json={"fields": {"labels": labels}},
        timeout=30,
    )
    if not resp.ok:
        print(f"Jira set_labels error {resp.status_code}: {resp.text}", file=sys.stderr)
        return False
    return True


def add_label(config: dict, issue_key: str, label: str) -> bool:
    """Add a label to a Jira issue (non-destructive)."""
    current = get_issue_labels(config, issue_key)
    if label in current:
        return True
    return set_issue_labels(config, issue_key, current + [label])


def remove_label(config: dict, issue_key: str, label: str) -> bool:
    """Remove a label from a Jira issue."""
    current = get_issue_labels(config, issue_key)
    if label not in current:
        return True
    return set_issue_labels(config, issue_key, [l for l in current if l != label])


def transition_label(config: dict, issue_key: str, from_label: str, to_label: str) -> None:
    """Remove one label and add another atomically (best-effort)."""
    current = get_issue_labels(config, issue_key)
    updated = [l for l in current if l != from_label]
    if to_label not in updated:
        updated.append(to_label)
    set_issue_labels(config, issue_key, updated)


def add_comment(config: dict, issue_key: str, body: str) -> bool:
    """Add a comment to a Jira issue."""
    url = f"{_base(config)}/rest/api/3/issue/{issue_key}/comment"
    resp = requests.post(
        url,
        headers=_auth_headers(config),
        json={"body": {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": body}]}
        ]}},
        timeout=30,
    )
    if not resp.ok:
        print(f"Jira add_comment error {resp.status_code}: {resp.text}", file=sys.stderr)
        return False
    return True
