"""Jira REST API client using basic auth (email + API token).

All operations use the Atlassian Cloud REST API v3.
Auth: base64(email:api_token) in Authorization header.

Required env (passed via config dict):
    JIRA_BASE_URL  — e.g. https://myorg.atlassian.net
    JIRA_EMAIL     — Atlassian account email
    JIRA_API_TOKEN — Jira API token
"""

import json
import sys
from base64 import b64encode
from typing import Any

import requests


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
        params={"jql": jql, "maxResults": max_results, "fields": "summary,description,labels,status"},
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
