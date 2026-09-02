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
from typing import Any

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


def search_issues(config: dict, jql: str, max_results: int = 20) -> list[dict]:
    """Search Jira issues by JQL. Returns list of issue dicts."""
    url = f"{_base(config)}/rest/api/3/search/jql"
    resp = requests.get(
        url,
        headers=_auth_headers(config),
        params={"jql": jql, "maxResults": max_results, "fields": "summary,description,labels,status,assignee,priority,issuetype,created"},
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
