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
    """Return open PRs relevant to the team — authored by or reviewing for team members.

    When STANDUP_TEAM_MEMBERS is set, only returns PRs where:
    - The author display name matches a team member, OR
    - A team member is listed as reviewer
    Returns [] silently when BB credentials are not configured.
    """
    ws = config.get("BITBUCKET_WORKSPACE", "")
    repo = config.get("BITBUCKET_REPO", "")
    if not ws or not repo:
        return []
    if not config.get("BITBUCKET_USERNAME") or not config.get("BITBUCKET_TOKEN"):
        return []

    raw_team = config.get("STANDUP_TEAM_MEMBERS", "").strip()
    if raw_team in ("<no value>", "{{.StandupTeamMembers}}"):
        raw_team = ""
    team_filter = [m.strip().lower() for m in raw_team.split(",") if m.strip()]

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
            print(f"[bb_helpers] PR list error {resp.status_code}: {resp.text[:200]}", file=sys.stderr, flush=True)
            break
        data = resp.json()
        for pr in data.get("values", []):
            author = pr.get("author", {}).get("display_name", "unknown")
            reviewers = [
                r["user"]["display_name"]
                for r in pr.get("reviewers", [])
                if r.get("user", {}).get("display_name")
            ]

            # Filter to team-relevant PRs
            if team_filter:
                is_team_author = author.lower() in team_filter
                is_team_reviewer = any(r.lower() in team_filter for r in reviewers)
                if not is_team_author and not is_team_reviewer:
                    continue

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
                "author": author,
                "branch": pr.get("source", {}).get("branch", {}).get("name", ""),
                "created": created,
                "age_hours": round(age_hours, 1),
                "reviewers": reviewers,
                "url": pr.get("links", {}).get("html", {}).get("href", ""),
            })
        url = data.get("next") or ""
        if not url:
            break
    return prs
