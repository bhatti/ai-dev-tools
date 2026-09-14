"""Issue enrichment: fetch attachments, linked issues, and linked PRs.

All functions are non-fatal — exceptions are caught, logged, and empty values returned.
Both Jira and GitHub paths live here so analyze scripts stay tracker-agnostic.

Config aliases: all credential reads go through config.get() so that config.py's
UPPER_SNAKE ↔ camelCase alias resolution handles both env vars and org-config keys.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from base64 import b64encode

import requests

_GH_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git|/|$)"
)
_BB_URL_RE = re.compile(
    r"https?://bitbucket\.org/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git|/|$)"
)

_MAX_ATTACHMENT_BYTES = 10 * 1024  # 10 KB
_ALLOWED_MIME_PREFIXES = ("text/", "application/json", "application/yaml", "application/x-yaml")
_BLOCKED_MIMES = ("image/", "application/pdf", "application/octet-stream",
                  "application/zip", "application/x-zip")

# ─── Jira helpers ────────────────────────────────────────────────────────────


def _jira_headers(config: dict) -> dict[str, str]:
    email = config.get("JIRA_EMAIL", "")
    token = config.get("JIRA_API_TOKEN", "")
    creds = b64encode(f"{email}:{token}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Accept": "application/json"}


def _jira_base(config: dict) -> str:
    return config.get("JIRA_BASE_URL", "").rstrip("/")


def fetch_jira_issue_full(config: dict, issue_key: str) -> dict | None:
    """GET /rest/api/3/issue/{key} with attachment and issuelinks fields."""
    try:
        url = f"{_jira_base(config)}/rest/api/3/issue/{issue_key}"
        resp = requests.get(
            url,
            headers=_jira_headers(config),
            params={
                "fields": (
                    "summary,description,labels,status,assignee,priority,"
                    "issuetype,created,attachment,issuelinks,comment"
                )
            },
            timeout=30,
        )
        if not resp.ok:
            print(f"[issue_fetcher] warn: fetch_jira_issue_full {issue_key}: "
                  f"{resp.status_code}", flush=True)
            return None
        return resp.json()
    except Exception as e:
        print(f"[issue_fetcher] warn: fetch_jira_issue_full {issue_key}: {e}", flush=True)
        return None


def get_jira_linked_prs(config: dict, issue_key: str, issue_id: str = "") -> list[dict]:
    """Query Jira dev-status API for PRs linked to an issue.

    Sends TWO requests — one for applicationType=bitbucket, one for applicationType=github —
    then merges the results. issue_id is the numeric Jira internal ID (issue["id"]).
    """
    if not issue_id:
        return []
    base = _jira_base(config)
    headers = _jira_headers(config)
    prs: list[dict] = []
    for app_type in ("bitbucket", "github"):
        try:
            resp = requests.get(
                f"{base}/rest/dev-status/latest/issue/detail",
                headers=headers,
                params={
                    "issueId": issue_id,
                    "applicationType": app_type,
                    "dataType": "pullrequest",
                },
                timeout=15,
            )
            if not resp.ok:
                continue
            data = resp.json()
            for detail in data.get("detail", []):
                for pr in detail.get("pullRequests", []):
                    pr["_app_type"] = app_type
                    prs.append(pr)
        except Exception as e:
            print(f"[issue_fetcher] warn: dev-status {app_type} for {issue_key}: {e}",
                  flush=True)
    return prs


def fetch_jira_attachment_text(config: dict, attachment: dict) -> str | None:
    """Download a Jira attachment and return its text content, or None to skip."""
    mime = attachment.get("mimeType", "")
    size = attachment.get("size", 0)
    content_url = attachment.get("content", "")

    if size > _MAX_ATTACHMENT_BYTES:
        return None
    if any(mime.startswith(b) for b in _BLOCKED_MIMES):
        return None
    if not any(mime.startswith(a) for a in _ALLOWED_MIME_PREFIXES):
        return None
    if not content_url:
        return None

    try:
        resp = requests.get(content_url, headers=_jira_headers(config), timeout=15)
        if resp.ok:
            return resp.text[:_MAX_ATTACHMENT_BYTES]
    except Exception as e:
        print(f"[issue_fetcher] warn: attachment download {content_url}: {e}", flush=True)
    return None


# ─── GitHub helpers ──────────────────────────────────────────────────────────


def _gh_env(config: dict) -> dict[str, str]:
    """Build env dict with GH_TOKEN for gh CLI calls."""
    env = dict(os.environ)
    token = config.get("GH_TOKEN", "")
    if token:
        env["GH_TOKEN"] = token
    return env


def fetch_gh_issue_full(config: dict, issue_number: str) -> dict | None:
    """Fetch a GitHub issue with body, comments, and linked PR references.

    Merges linked_prs from 'gh pr list --search "#{n}"' into issue["linked_prs"].
    """
    org = config.get("GH_ORG", "")
    repo = config.get("GH_REPO", "")
    if not org or not repo:
        return None

    env = _gh_env(config)
    try:
        result = subprocess.run(
            [
                "gh", "issue", "view", str(issue_number),
                "--repo", f"{org}/{repo}",
                "--json", "number,title,url,labels,assignees,state,body,comments",
            ],
            capture_output=True, text=True, timeout=30, env=env,
        )
        if result.returncode != 0 or not result.stdout.strip():
            print(f"[issue_fetcher] warn: gh issue view {issue_number}: "
                  f"{result.stderr.strip()[:200]}", flush=True)
            return None
        issue = json.loads(result.stdout)
    except Exception as e:
        print(f"[issue_fetcher] warn: fetch_gh_issue_full #{issue_number}: {e}", flush=True)
        return None

    # Fetch linked PRs (non-fatal)
    try:
        pr_result = subprocess.run(
            [
                "gh", "pr", "list",
                "--repo", f"{org}/{repo}",
                "--search", f"#{issue_number}",
                "--state", "all",
                "--limit", "10",
                "--json", "number,title,url,state,mergedAt",
            ],
            capture_output=True, text=True, timeout=20, env=env,
        )
        if pr_result.returncode == 0 and pr_result.stdout.strip():
            issue["linked_prs"] = json.loads(pr_result.stdout)
        else:
            issue["linked_prs"] = []
    except Exception:
        issue["linked_prs"] = []

    return issue


# ─── Repo detection ──────────────────────────────────────────────────────────


def detect_repo_from_issue(issue_data: dict | None, config: dict) -> dict | None:
    """Scan issue body/description for a github.com or bitbucket.org repo URL.

    Returns {"tracker": "github"|"bitbucket", "org": str, "repo": str} or None.
    Returns None immediately if the relevant env vars are already configured so
    that an explicitly configured repo always wins.
    """
    # If Bitbucket or GitHub is already configured, don't override via issue body
    if config.get("BITBUCKET_WORKSPACE") and config.get("BITBUCKET_REPO"):
        return None
    if config.get("GH_ORG") and config.get("GH_REPO"):
        return None
    if issue_data is None:
        return None

    # Collect candidate text from the issue
    body_text = ""
    fields = issue_data.get("fields", {})  # Jira shape
    if fields:
        desc = fields.get("description") or ""
        if isinstance(desc, dict):
            from scripts.common.jira_api import extract_adf_text
            body_text = extract_adf_text(desc)
        else:
            body_text = str(desc)
        for link in fields.get("issuelinks", []):
            for sub_key in ("inwardIssue", "outwardIssue"):
                linked = link.get(sub_key, {})
                body_text += " " + str((linked.get("fields") or {}).get("description") or "")
    else:
        # GitHub shape
        body_text = str(issue_data.get("body") or "")

    # Search GitHub URLs first, then Bitbucket
    for m in _GH_URL_RE.finditer(body_text):
        org, repo = m.group(1), m.group(2)
        if org and repo:
            return {"tracker": "github", "org": org, "repo": repo}
    for m in _BB_URL_RE.finditer(body_text):
        workspace, repo = m.group(1), m.group(2)
        if workspace and repo:
            return {"tracker": "bitbucket", "org": workspace, "repo": repo}
    return None


