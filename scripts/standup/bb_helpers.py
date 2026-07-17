"""Bitbucket helpers for standup signal gathering.

Wraps scripts/common/bitbucket_api.py with the richer shape needed for
standup (age_hours, reviewers list) and graceful degradation when BB
credentials are absent.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import requests
from scripts.common.bitbucket_api import _auth, _BASE


def get_open_prs(config: dict) -> list[dict]:
    """Return open PRs enriched with age and reviewers.

    Returns [] silently when BB credentials are not configured so the
    standup brief still works without Bitbucket.
    """
    ws = config.get("BITBUCKET_WORKSPACE", "")
    repo = config.get("BITBUCKET_REPO", "")
    if not ws or not repo:
        return []
    if not config.get("BITBUCKET_USERNAME") or not config.get("BITBUCKET_TOKEN"):
        return []

    url = f"{_BASE}/repositories/{ws}/{repo}/pullrequests"
    prs: list[dict] = []
    for _ in range(5):          # max 5 pages = 250 PRs
        resp = requests.get(
            url,
            auth=_auth(config),
            params={"state": "OPEN", "pagelen": 50},
            timeout=30,
        )
        if not resp.ok:
            print(f"[bb_helpers] PR list error {resp.status_code}", flush=True)
            break
        data = resp.json()
        for pr in data.get("values", []):
            created = pr.get("created_on", "")
            try:
                age_hours = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(created.replace("Z", "+00:00"))
                ).total_seconds() / 3600
            except (ValueError, AttributeError):
                age_hours = 0
            prs.append({
                "id": pr["id"],
                "title": pr.get("title", ""),
                "author": pr.get("author", {}).get("display_name", "unknown"),
                "created": created,
                "age_hours": round(age_hours, 1),
                "reviewers": [
                    r["user"]["display_name"]
                    for r in pr.get("reviewers", [])
                    if r.get("user", {}).get("display_name")
                ],
                "url": pr.get("links", {}).get("html", {}).get("href", ""),
            })
        url = data.get("next") or ""
        if not url:
            break
    return prs
