"""Gather sprint PRs for the PR queue skill.

Approach (Jira/Bitbucket):
  1. Fetch the active sprint issues for the current user from Jira
  2. For each issue, query the Jira dev-status API to get linked Bitbucket PRs
     (GET /rest/dev-status/latest/issue/detail?issueId={id}&applicationType=bitbucket&dataType=pullrequest)
  3. Filter to OPEN PRs only, deduplicate across issues
  4. Write /workspace/pr_queue.json

  Using the Jira dev-status API avoids scanning 700+ open Bitbucket PRs — it
  directly returns the PRs that developers linked to each Jira issue via commits
  or branch names, with reviewer/approval state included.

Approach (GitHub):
  1. Fetch all open PRs via gh CLI
  2. Write /workspace/pr_queue.json with reviewer/approval state

Tracker is selected by DEFAULT_TRACKER env var ("jira" or "github").
Fallback inference: Jira when JIRA_BASE_URL+JIRA_PROJECT are set, else GitHub.

Exit codes: 0=success, 1=error
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from scripts.common.config import get_workspace_dir, load_config
from scripts.standup.gather_jira import (
    get_active_sprints,
    get_sprint_issues,
    search_open_issues,
    _jira_headers,
    _jira_base,
)

_JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9_]+-\d+)\b")


def _get_sprint_issues_with_ids(config: dict, workspace_dir: Path | None = None) -> tuple[list[dict], str]:
    """Return (list_of_issue_dicts, sprint_name) for the active sprint.

    Each issue dict has at minimum: key, id (numeric), summary, status.

    Fast path: reuse signals.json if already written by gather_jira.
    Falls back to Jira API when signals.json is absent or lacks numeric IDs.
    """
    if workspace_dir is None:
        workspace_dir = Path(config.get("WORKSPACE_DIR", "/workspace"))

    signals_path = workspace_dir / "signals.json"
    if signals_path.exists():
        try:
            signals = json.loads(signals_path.read_text())
            sprint_name = (signals.get("sprint") or {}).get("name", "") or ""
            issues = [
                i for i in signals.get("issues", [])
                if i.get("key") and i.get("id")
            ]
            if issues:
                print(
                    f"[gather_pr_queue] loaded {len(issues)} sprint issues from signals.json "
                    f"(sprint={sprint_name!r})",
                    flush=True,
                )
                return issues, sprint_name
        except Exception as e:
            print(f"[gather_pr_queue] warn: could not read signals.json: {e}", flush=True)

    active_sprints = get_active_sprints(config)
    sprint_name = ""
    issues: list[dict] = []

    if active_sprints:
        sprint = active_sprints[0]
        sprint_name = sprint.get("name", "")
        sprint_id = sprint["id"]
        print(f"[gather_pr_queue] active sprint: {sprint_name!r} (id={sprint_id})", flush=True)
        for raw in get_sprint_issues(config, sprint_id):
            key = raw.get("key", "")
            numeric_id = str(raw.get("id", ""))
            if key and numeric_id:
                fields = raw.get("fields", {}) or {}
                issues.append({
                    "key": key,
                    "id": numeric_id,
                    "summary": (fields.get("summary") or "")[:80],
                    "status": ((fields.get("status") or {}).get("name") or ""),
                })

    if not issues:
        print("[gather_pr_queue] no sprint issues found, falling back to open issues", flush=True)
        for raw in search_open_issues(config):
            key = raw.get("key", "")
            numeric_id = str(raw.get("id", ""))
            if key and numeric_id:
                fields = raw.get("fields", {}) or {}
                issues.append({
                    "key": key,
                    "id": numeric_id,
                    "summary": (fields.get("summary") or "")[:80],
                    "status": ((fields.get("status") or {}).get("name") or ""),
                })

    return issues, sprint_name


def _get_prs_for_issue(config: dict, issue_id: str) -> list[dict]:
    """Query Jira dev-status API for Bitbucket PRs linked to a single issue.

    Returns a list of PR dicts with id, name, url, status, author, reviewers.
    """
    base = _jira_base(config)
    headers = _jira_headers(config)
    try:
        resp = requests.get(
            f"{base}/rest/dev-status/latest/issue/detail",
            headers=headers,
            params={
                "issueId": issue_id,
                "applicationType": "bitbucket",
                "dataType": "pullrequest",
            },
            timeout=15,
        )
        if not resp.ok:
            return []
        data = resp.json()
        prs: list[dict] = []
        for detail in data.get("detail", []):
            for pr in detail.get("pullRequests", []):
                prs.append(pr)
        return prs
    except Exception as e:
        print(f"[gather_pr_queue] warn: dev-status for issue {issue_id}: {e}", flush=True)
        return []


def _gather_jira(config: dict, workspace_dir: Path | None = None) -> dict:
    """Gather PR queue from Jira dev-status API. Returns result dict."""
    jira_url = config.get("JIRA_BASE_URL", "")
    jira_project = config.get("JIRA_PROJECT", "")
    if not jira_url or not jira_project:
        print(
            "[gather_pr_queue] ERROR: JIRA_BASE_URL and JIRA_PROJECT required for jira tracker",
            file=sys.stderr,
        )
        sys.exit(1)

    print("[gather_pr_queue] fetching sprint issues ...", flush=True)
    sprint_issues, sprint_name = _get_sprint_issues_with_ids(config, workspace_dir)
    print(f"[gather_pr_queue] sprint: {sprint_name!r}, {len(sprint_issues)} issues", flush=True)

    if not sprint_issues:
        return {"sprint": sprint_name, "pr_count": 0, "prs": []}

    # Query dev-status per issue — collect OPEN PRs, deduplicate by PR id
    print("[gather_pr_queue] querying Jira dev-status API for linked PRs ...", flush=True)
    seen_pr_ids: set[str] = set()
    output_prs: list[dict] = []

    for issue in sprint_issues:
        issue_key = issue["key"]
        issue_id = issue["id"]
        raw_prs = _get_prs_for_issue(config, issue_id)

        for pr in raw_prs:
            pr_id = str(pr.get("id", ""))
            pr_status = (pr.get("status") or "").upper()
            if pr_status != "OPEN":
                continue
            if pr_id in seen_pr_ids:
                continue
            seen_pr_ids.add(pr_id)

            # Parse age from lastUpdate or created
            age_days: float = 0.0
            for ts_field in ("created", "lastUpdate"):
                ts = pr.get(ts_field, "")
                if ts:
                    try:
                        age_days = (
                            datetime.now(timezone.utc)
                            - datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        ).total_seconds() / 86400
                        break
                    except (ValueError, AttributeError):
                        pass

            # Reviewers — dev-status returns {name, approved} per reviewer
            reviewers_raw = pr.get("reviewers", [])
            approved_by: list[str] = [
                r.get("name", "") for r in reviewers_raw if r.get("approved")
            ]
            pending_reviewers: list[str] = [
                r.get("name", "") for r in reviewers_raw if not r.get("approved")
            ]

            # Fix Bitbucket URL — dev-status uses UUID placeholders like {uuid}, replace with real slug
            pr_url = pr.get("url", "")
            if re.search(r"\{[0-9a-f-]{36}\}", pr_url, re.IGNORECASE):
                bb_ws = config.get("BITBUCKET_WORKSPACE", "")
                bb_repo = config.get("BITBUCKET_REPO", "")
                if bb_ws and bb_repo:
                    pr_url = f"https://bitbucket.org/{bb_ws}/{bb_repo}/pull-requests/{pr_id}"

            jira_base = _jira_base(config).rstrip("/")
            jira_url = f"{jira_base}/browse/{issue_key}" if jira_base and issue_key else ""

            output_prs.append({
                "id": pr_id,
                "title": pr.get("name", ""),
                "author": (pr.get("author") or {}).get("name", "unknown"),
                "url": pr_url,
                "jira_url": jira_url,
                "age_days": round(age_days, 1),
                "jira_key": issue_key,
                "jira_summary": issue.get("summary", ""),
                "jira_status": issue.get("status", ""),
                "reviewers": pending_reviewers,
                "approved_by": approved_by,
                "changes_requested_by": [],
            })

    print(f"[gather_pr_queue] {len(output_prs)} open PRs linked to sprint issues", flush=True)
    return {
        "sprint": sprint_name,
        "pr_count": len(output_prs),
        "prs": output_prs,
    }


def _gather_github(config: dict) -> dict:
    """Gather PR queue from GitHub via gh CLI. Returns result dict."""
    from scripts.standup.gather_gh import get_open_prs as gh_get_open_prs

    gh_org = config.get("GH_ORG", "")
    gh_repo = config.get("GH_REPO", "")
    if not gh_org or not gh_repo:
        print("[gather_pr_queue] ERROR: GH_ORG and GH_REPO required for github tracker", file=sys.stderr)
        sys.exit(1)

    print(f"[gather_pr_queue] fetching open GitHub PRs for {gh_org}/{gh_repo} ...", flush=True)
    raw_prs = gh_get_open_prs(config)
    print(f"[gather_pr_queue] {len(raw_prs)} total open PRs", flush=True)

    output_prs = []
    for pr in raw_prs:
        age_days = round(pr["age_hours"] / 24, 1)

        review_states = pr.get("review_states", [])
        approved_by: list[str] = []
        changes_requested_by: list[str] = []
        pending_reviewers: list[str] = pr.get("reviewers", [])

        if "APPROVED" in review_states:
            approved_by = [f"{review_states.count('APPROVED')} approved"]
        if "CHANGES_REQUESTED" in review_states:
            changes_requested_by = ["changes requested"]

        jira_keys = _JIRA_KEY_RE.findall(pr.get("title", ""))
        jira_key = jira_keys[0] if jira_keys else ""

        output_prs.append({
            "id": pr["id"],
            "title": pr["title"],
            "author": pr["author"],
            "url": pr["url"],
            "age_days": age_days,
            "jira_key": jira_key,
            "jira_summary": "",
            "jira_status": "",
            "reviewers": pending_reviewers,
            "approved_by": approved_by,
            "changes_requested_by": changes_requested_by,
        })

    return {
        "sprint": f"{gh_org}/{gh_repo}",
        "pr_count": len(output_prs),
        "prs": output_prs,
    }


def main() -> None:
    config = load_config(required=[])
    workspace_dir = get_workspace_dir(config)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    default_tracker = config.get("DEFAULT_TRACKER", "").lower().strip()
    if default_tracker not in ("jira", "github"):
        if config.get("JIRA_BASE_URL") and config.get("JIRA_PROJECT"):
            default_tracker = "jira"
        elif config.get("GH_ORG") and config.get("GH_REPO"):
            default_tracker = "github"
        else:
            print(
                "[gather_pr_queue] ERROR: cannot determine tracker — set DEFAULT_TRACKER=jira or DEFAULT_TRACKER=github",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"[gather_pr_queue] tracker={default_tracker}", flush=True)

    if default_tracker == "jira":
        result = _gather_jira(config, workspace_dir)
    else:
        result = _gather_github(config)

    out_path = workspace_dir / "pr_queue.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"[gather_pr_queue] wrote {out_path} ({result['pr_count']} PRs)", flush=True)


if __name__ == "__main__":
    main()
