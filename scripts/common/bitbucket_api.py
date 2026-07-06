"""BitBucket Cloud REST API v2 client using basic auth (username + app password).

Required config keys:
    BITBUCKET_USERNAME  — BitBucket account email (NOT the username/nickname —
                          Bitbucket's REST API v2 requires the email address for
                          Basic Auth with app passwords)
    BITBUCKET_TOKEN     — BitBucket App Password
"""

import sys
from typing import Any

import requests

_BASE = "https://api.bitbucket.org/2.0"


def _auth(config: dict) -> tuple[str, str]:
    return (config["BITBUCKET_USERNAME"], config["BITBUCKET_TOKEN"])


def _repo(workspace: str, repo: str) -> str:
    return f"{_BASE}/repositories/{workspace}/{repo}"


# --------------------------------------------------------------------------
# Pull Requests
# --------------------------------------------------------------------------

def create_pr(
    config: dict,
    workspace: str,
    repo: str,
    title: str,
    description: str,
    source_branch: str,
    dest_branch: str = "main",
    close_source_branch: bool = True,
) -> dict | None:
    """Create a BitBucket PR. Returns the PR dict or None on failure."""
    url = f"{_repo(workspace, repo)}/pullrequests"
    payload = {
        "title": title,
        "description": description,
        "source": {"branch": {"name": source_branch}},
        "destination": {"branch": {"name": dest_branch}},
        "close_source_branch": close_source_branch,
    }
    resp = requests.post(url, auth=_auth(config), json=payload, timeout=30)
    if not resp.ok:
        print(f"BitBucket create_pr error {resp.status_code}: {resp.text}", file=sys.stderr)
        return None
    return resp.json()


def get_pr(config: dict, workspace: str, repo: str, pr_id: int | str) -> dict | None:
    """Fetch a single PR by ID."""
    url = f"{_repo(workspace, repo)}/pullrequests/{pr_id}"
    resp = requests.get(url, auth=_auth(config), timeout=30)
    if not resp.ok:
        return None
    return resp.json()


def get_pr_state(config: dict, workspace: str, repo: str, pr_id: int | str) -> str:
    """Return PR state string: OPEN, MERGED, DECLINED, SUPERSEDED, or UNKNOWN."""
    pr = get_pr(config, workspace, repo, pr_id)
    if not pr:
        return "UNKNOWN"
    return pr.get("state", "UNKNOWN").upper()


def list_pr_comments(config: dict, workspace: str, repo: str, pr_id: int | str) -> list[dict]:
    """Return all comments on a PR (paginated)."""
    url = f"{_repo(workspace, repo)}/pullrequests/{pr_id}/comments"
    comments: list[dict] = []
    max_pages = 50
    for _ in range(max_pages):
        if not url:
            break
        resp = requests.get(url, auth=_auth(config), timeout=30)
        if not resp.ok:
            break
        data = resp.json()
        comments.extend(data.get("values", []))
        url = data.get("next")
    return comments


def add_pr_comment(
    config: dict, workspace: str, repo: str, pr_id: int | str, body: str
) -> bool:
    """Post a comment on a PR."""
    url = f"{_repo(workspace, repo)}/pullrequests/{pr_id}/comments"
    resp = requests.post(
        url,
        auth=_auth(config),
        json={"content": {"raw": body}},
        timeout=30,
    )
    if not resp.ok:
        print(f"BitBucket add_comment error {resp.status_code}: {resp.text}", file=sys.stderr)
        return False
    return True
