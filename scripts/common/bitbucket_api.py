"""BitBucket Cloud REST API v2 client using basic auth (username + app password).

Required config keys:
    BITBUCKET_USERNAME  — BitBucket account username (used for both REST API Basic Auth
                          and git HTTPS clone/push credential helper)
    BITBUCKET_TOKEN     — BitBucket App Password
"""

import os
import sys
from typing import Any

import requests

# BITBUCKET_API_BASE points the client at a proxy or gateway; the default is Bitbucket Cloud.
def api_base() -> str:
    """Bitbucket REST base URL, read from the environment at call time."""
    return os.environ.get("BITBUCKET_API_BASE", "https://api.bitbucket.org/2.0").rstrip("/")


def auth(config: dict) -> tuple[str, str]:
    token = config.get("BITBUCKET_TOKEN") or config.get("BITBUCKET_APP_PASSWORD") or ""
    return (config["BITBUCKET_USERNAME"], token)


def _repo(workspace: str, repo: str) -> str:
    return f"{api_base()}/repositories/{workspace}/{repo}"


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
    resp = requests.post(url, auth=auth(config), json=payload, timeout=30)
    if not resp.ok:
        print(f"BitBucket create_pr error {resp.status_code}: {resp.text}", file=sys.stderr)
        return None
    return resp.json()


class BitbucketNetworkError(IOError):
    """Transient network failure — caller should retry rather than hard-fail."""


def get_pr(config: dict, workspace: str, repo: str, pr_id: int | str) -> dict | None:
    """Fetch a single PR by ID. Raises BitbucketNetworkError on timeout/connection failure."""
    url = f"{_repo(workspace, repo)}/pullrequests/{pr_id}"
    try:
        resp = requests.get(url, auth=auth(config), timeout=30)
    except requests.exceptions.Timeout as exc:
        raise BitbucketNetworkError(f"Bitbucket API timed out fetching PR {pr_id}") from exc
    except requests.exceptions.ConnectionError as exc:
        raise BitbucketNetworkError(f"Bitbucket API connection failed for PR {pr_id}") from exc
    if not resp.ok:
        return None
    return resp.json()


def get_pr_state(config: dict, workspace: str, repo: str, pr_id: int | str) -> str:
    """Return PR state string: OPEN, MERGED, DECLINED, SUPERSEDED, or UNKNOWN.

    Propagates BitbucketNetworkError so callers can distinguish transient failures
    (retry/pause) from permanent errors (token expired, PR not found).
    """
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
        resp = requests.get(url, auth=auth(config), timeout=30)
        if not resp.ok:
            break
        data = resp.json()
        comments.extend(data.get("values", []))
        url = data.get("next")
    return comments


def add_pr_comment(
    config: dict, workspace: str, repo: str, pr_id: int | str, body: str,
    parent_id: int | str | None = None,
) -> bool:
    """Post a comment on a PR, optionally as a threaded reply to parent_id."""
    url = f"{_repo(workspace, repo)}/pullrequests/{pr_id}/comments"
    payload: dict = {"content": {"raw": body}}
    if parent_id:
        payload["parent"] = {"id": int(parent_id)}
    resp = requests.post(url, auth=auth(config), json=payload, timeout=30)
    if not resp.ok:
        print(f"BitBucket add_comment error {resp.status_code}: {resp.text}", file=sys.stderr)
        return False
    return True
