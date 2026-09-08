"""Fetch merged PRs with comments from GitHub or Bitbucket.

Normalizes PR data to a common schema regardless of provider.
Used by run_pr_audit.py to build the audit context.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

KNOWN_BOTS = {
    "github-actions[bot]", "dependabot[bot]", "renovate[bot]",
    "codecov[bot]", "sonarcloud[bot]", "mergify[bot]",
    "copilot[bot]", "netlify[bot]", "vercel[bot]",
}
_KNOWN_BOTS_LOWER = frozenset(b.lower() for b in KNOWN_BOTS)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def fetch_prs(config: dict, n_prs: int = 50, max_bytes: int = 10_000_000) -> list[dict]:
    """Dispatch to GitHub or Bitbucket based on DEFAULT_TRACKER."""
    tracker = (config.get("DEFAULT_TRACKER") or "").lower().strip()
    if tracker in ("jira", "jira/bitbucket", "bitbucket"):
        return fetch_bitbucket_prs(config, n_prs)
    return fetch_github_prs(config, n_prs)


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def fetch_github_prs(config: dict, n_prs: int = 50) -> list[dict]:
    """Fetch last N merged PRs via ``gh pr list --state merged``.

    Enriches each PR with inline review comments via the GitHub API.
    """
    org = config.get("GH_ORG", "").strip()
    repo = config.get("GH_REPO", "").strip()
    if not org or not repo:
        print("[pr-fetch] GH_ORG/GH_REPO not set — cannot fetch GitHub PRs", file=sys.stderr, flush=True)
        return []

    fields = (
        "number,title,body,author,mergedAt,url,headRefName,"
        "comments,reviews,reviewDecision,labels,files"
    )
    cmd = [
        "gh", "pr", "list",
        "-R", f"{org}/{repo}",
        "--state", "merged",
        "--limit", str(n_prs),
        "--json", fields,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[pr-fetch] gh pr list failed: {e}", file=sys.stderr, flush=True)
        return []

    if result.returncode != 0:
        print(f"[pr-fetch] gh pr list error: {result.stderr.strip()[:300]}", file=sys.stderr, flush=True)
        return []

    try:
        raw_prs = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"[pr-fetch] could not parse gh output: {e}", file=sys.stderr, flush=True)
        return []

    prs: list[dict] = []
    for rp in raw_prs:
        # Collect all comments (PR-level + review bodies)
        all_comments: list[dict] = []
        for c in rp.get("comments", []):
            all_comments.append({
                "author": c.get("author", {}).get("login", ""),
                "body": c.get("body", ""),
                "type": "comment",
            })
        for rv in rp.get("reviews", []):
            body = rv.get("body", "").strip()
            if body:
                all_comments.append({
                    "author": rv.get("author", {}).get("login", ""),
                    "body": body,
                    "type": "review",
                })

        # Fetch inline review comments (code-level)
        inline = _fetch_gh_review_comments(org, repo, rp.get("number", 0))
        all_comments.extend(inline)

        classified = classify_comments(all_comments)

        files_list = rp.get("files", []) or []
        files_changed = len(files_list) if isinstance(files_list, list) else 0
        additions = sum(f.get("additions", 0) for f in files_list if isinstance(f, dict))
        deletions = sum(f.get("deletions", 0) for f in files_list if isinstance(f, dict))
        file_paths = [f.get("path", "") for f in files_list if isinstance(f, dict)]

        pr = {
            "number": rp.get("number", 0),
            "title": rp.get("title", ""),
            "author": rp.get("author", {}).get("login", ""),
            "merged_at": rp.get("mergedAt", ""),
            "url": rp.get("url", ""),
            "branch": rp.get("headRefName", ""),
            "body": rp.get("body", ""),
            "files_changed": files_changed,
            "additions": additions,
            "deletions": deletions,
            "file_paths": file_paths[:50],
            "all_comments": all_comments,
            "bot_comments": classified["bot_comments"],
            "human_comments": classified["human_comments"],
            "linked_issue": None,
            "review_decision": rp.get("reviewDecision", ""),
        }
        prs.append(pr)

    print(f"[pr-fetch] fetched {len(prs)} merged PRs from GitHub ({org}/{repo})", flush=True)
    return prs


def _fetch_gh_review_comments(org: str, repo: str, pr_number: int) -> list[dict]:
    """Fetch inline review comments for a single PR."""
    if not pr_number:
        return []
    cmd = [
        "gh", "api",
        f"repos/{org}/{repo}/pulls/{pr_number}/comments",
        "--paginate", "--jq", ".",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        # --paginate can produce concatenated arrays; try merging them
        try:
            text = "[" + result.stdout.strip().replace("]\n[", ",").replace("][", ",") + "]"
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return []
    comments = []
    for c in data if isinstance(data, list) else []:
        comments.append({
            "author": c.get("user", {}).get("login", ""),
            "body": c.get("body", ""),
            "type": "review_comment",
            "path": c.get("path", ""),
            "diff_hunk": c.get("diff_hunk", "")[:200],
        })
    return comments


# ---------------------------------------------------------------------------
# Bitbucket
# ---------------------------------------------------------------------------

def fetch_bitbucket_prs(config: dict, n_prs: int = 50) -> list[dict]:
    """Fetch last N merged PRs via Bitbucket REST API."""
    import requests as _requests

    workspace = config.get("BITBUCKET_WORKSPACE", "").strip()
    repo = config.get("BITBUCKET_REPO", "").strip()
    if not workspace or not repo:
        print("[pr-fetch] BITBUCKET_WORKSPACE/REPO not set", file=sys.stderr, flush=True)
        return []

    username = config.get("BITBUCKET_USERNAME", "").strip()
    token = config.get("BITBUCKET_TOKEN", config.get("BITBUCKET_APP_PASSWORD", "")).strip()
    auth = (username, token) if username and token else None

    base = f"https://api.bitbucket.org/2.0/repositories/{workspace}/{repo}"
    url = f"{base}/pullrequests?state=MERGED&sort=-updated_on&pagelen=50"

    raw_prs: list[dict] = []
    while url and len(raw_prs) < n_prs:
        try:
            resp = _requests.get(url, auth=auth, timeout=30)
            resp.raise_for_status()
        except Exception as e:
            print(f"[pr-fetch] Bitbucket API error: {e}", file=sys.stderr, flush=True)
            break
        data = resp.json()
        raw_prs.extend(data.get("values", []))
        url = data.get("next", "")

    prs: list[dict] = []
    for rp in raw_prs[:n_prs]:
        pr_id = rp.get("id", 0)
        # Fetch comments
        all_comments = _fetch_bb_comments(base, pr_id, auth)
        classified = classify_comments(all_comments)

        # Fetch diffstat for file-level metrics
        diffstat = _fetch_bb_diffstat(base, pr_id, auth)
        files_changed = len(diffstat)
        additions = sum(d.get("lines_added", 0) for d in diffstat)
        deletions = sum(d.get("lines_removed", 0) for d in diffstat)
        file_paths = [d.get("path", "") for d in diffstat]

        pr = {
            "number": pr_id,
            "title": rp.get("title", ""),
            "author": rp.get("author", {}).get("display_name", rp.get("author", {}).get("nickname", "")),
            "merged_at": rp.get("updated_on", ""),
            "url": rp.get("links", {}).get("html", {}).get("href", ""),
            "branch": rp.get("source", {}).get("branch", {}).get("name", ""),
            "body": rp.get("description", ""),
            "files_changed": files_changed,
            "additions": additions,
            "deletions": deletions,
            "file_paths": file_paths[:50],
            "all_comments": all_comments,
            "bot_comments": classified["bot_comments"],
            "human_comments": classified["human_comments"],
            "linked_issue": None,
            "review_decision": "",
        }
        prs.append(pr)

    print(f"[pr-fetch] fetched {len(prs)} merged PRs from Bitbucket ({workspace}/{repo})", flush=True)
    return prs


def _fetch_bb_comments(base_url: str, pr_id: int, auth: tuple | None) -> list[dict]:
    """Fetch comments for a Bitbucket PR."""
    import requests as _requests

    url = f"{base_url}/pullrequests/{pr_id}/comments?pagelen=100"
    comments: list[dict] = []
    try:
        resp = _requests.get(url, auth=auth, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        for c in data.get("values", []):
            user = c.get("user", {})
            comments.append({
                "author": user.get("display_name", user.get("nickname", "")),
                "body": c.get("content", {}).get("raw", ""),
                "type": "comment",
            })
    except Exception:
        pass
    return comments


def _fetch_bb_diffstat(base_url: str, pr_id: int, auth: tuple | None) -> list[dict]:
    """Fetch diffstat (file-level additions/deletions) for a Bitbucket PR."""
    import requests as _requests

    url = f"{base_url}/pullrequests/{pr_id}/diffstat?pagelen=100"
    files: list[dict] = []
    try:
        resp = _requests.get(url, auth=auth, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        for entry in data.get("values", []):
            new_file = entry.get("new", {}) or {}
            old_file = entry.get("old", {}) or {}
            path = new_file.get("path", "") or old_file.get("path", "")
            files.append({
                "path": path,
                "lines_added": entry.get("lines_added", 0),
                "lines_removed": entry.get("lines_removed", 0),
            })
    except Exception:
        pass
    return files


# ---------------------------------------------------------------------------
# Comment classification
# ---------------------------------------------------------------------------

def classify_comments(comments: list[dict]) -> dict:
    """Separate bot vs human comments.

    Bot: username ends with 'bot' or '[bot]' (case-insensitive), or in KNOWN_BOTS.
    Returns {'bot_comments': [...], 'human_comments': [...]}.
    """
    bot: list[dict] = []
    human: list[dict] = []
    for c in comments:
        author = c.get("author", "")
        if _is_bot(author):
            bot.append(c)
        else:
            human.append(c)
    return {"bot_comments": bot, "human_comments": human}


def _is_bot(author: str) -> bool:
    if not author:
        return False
    lower = author.lower()
    if lower in _KNOWN_BOTS_LOWER:
        return True
    if lower.endswith("[bot]") or lower.endswith("bot"):
        return True
    return False


# ---------------------------------------------------------------------------
# Issue linking
# ---------------------------------------------------------------------------

def link_pr_to_issue(pr: dict, config: dict) -> dict | None:
    """Extract linked issue reference from PR title/body.

    Jira: r'([A-Z][A-Z0-9]+-\\d+)'
    GitHub: r'(?:closes?|fixes?|resolves?)\\s*#(\\d+)' or just r'#(\\d+)'
    Returns {'key': '...', 'source': 'jira'|'github', 'url': '...'} or None.
    """
    text = f"{pr.get('title', '')} {pr.get('body', '')}"

    # Jira pattern
    m = re.search(r'([A-Z][A-Z0-9]+-\d+)', text)
    if m:
        key = m.group(1)
        jira_url = config.get("JIRA_BASE_URL", "").rstrip("/")
        url = f"{jira_url}/browse/{key}" if jira_url else ""
        return {"key": key, "source": "jira", "url": url}

    # GitHub closing keywords
    m = re.search(r'(?:closes?|fixes?|resolves?)\s*#(\d+)', text, re.IGNORECASE)
    if m:
        num = m.group(1)
        org = config.get("GH_ORG", "")
        repo = config.get("GH_REPO", "")
        url = f"https://github.com/{org}/{repo}/issues/{num}" if org and repo else ""
        return {"key": f"#{num}", "source": "github", "url": url}

    # Bare issue reference
    m = re.search(r'#(\d+)', text)
    if m:
        num = m.group(1)
        org = config.get("GH_ORG", "")
        repo = config.get("GH_REPO", "")
        url = f"https://github.com/{org}/{repo}/issues/{num}" if org and repo else ""
        return {"key": f"#{num}", "source": "github", "url": url}

    return None


# ---------------------------------------------------------------------------
# Issue detail fetching
# ---------------------------------------------------------------------------

def fetch_issue_details(issue_ref: dict, config: dict) -> dict | None:
    """Fetch issue details from Jira or GitHub.

    Returns {'title', 'body', 'labels', 'acceptance_criteria', 'design_doc_links'} or None.
    """
    if not issue_ref:
        return None
    source = issue_ref.get("source", "")
    if source == "github":
        return _fetch_github_issue(issue_ref, config)
    if source == "jira":
        return _fetch_jira_issue(issue_ref, config)
    return None


def _fetch_github_issue(issue_ref: dict, config: dict) -> dict | None:
    key = issue_ref.get("key", "").lstrip("#")
    if not key:
        return None
    org = config.get("GH_ORG", "")
    repo = config.get("GH_REPO", "")
    if not org or not repo:
        return None
    cmd = [
        "gh", "issue", "view", key,
        "-R", f"{org}/{repo}",
        "--json", "title,body,labels",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    body = data.get("body", "")
    return {
        "title": data.get("title", ""),
        "body": body,
        "labels": [lb.get("name", "") for lb in data.get("labels", [])],
        "acceptance_criteria": _extract_section(body, "acceptance criteria"),
        "design_doc_links": _extract_links(body),
    }


def _fetch_jira_issue(issue_ref: dict, config: dict) -> dict | None:
    import requests as _requests

    key = issue_ref.get("key", "")
    jira_url = config.get("JIRA_BASE_URL", "").rstrip("/")
    email = config.get("JIRA_EMAIL", "")
    token = config.get("JIRA_API_TOKEN", "")
    if not all([key, jira_url, email, token]):
        return None
    try:
        resp = _requests.get(
            f"{jira_url}/rest/api/2/issue/{key}",
            auth=(email, token),
            timeout=20,
        )
        resp.raise_for_status()
    except Exception:
        return None
    data = resp.json()
    fields = data.get("fields", {})
    description = fields.get("description", "") or ""
    return {
        "title": fields.get("summary", ""),
        "body": description,
        "labels": fields.get("labels", []),
        "acceptance_criteria": _extract_section(description, "acceptance criteria"),
        "design_doc_links": _extract_links(description),
    }


def _extract_section(text: str, heading: str) -> str:
    """Extract a section from markdown text by heading name."""
    if not text:
        return ""
    pattern = re.compile(
        rf'#+\s*{re.escape(heading)}\s*\n(.*?)(?=\n#+\s|\Z)',
        re.IGNORECASE | re.DOTALL,
    )
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def _extract_links(text: str) -> list[str]:
    """Extract URLs from text."""
    if not text:
        return []
    return re.findall(r'https?://[^\s<>\)]+', text)


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def build_pr_context(prs: list[dict], max_chars: int = 100_000) -> str:
    """Serialize PR data as structured Markdown for the Claude prompt.

    Per PR: number, title, author, merged date, files changed count,
    linked issue summary, bot comments summary, human comments summary.
    Truncates per-PR to stay within the total character budget.
    """
    if not prs:
        return "(no PRs available)"

    per_pr_budget = max(max_chars // max(len(prs), 1), 500)
    lines: list[str] = [f"## Merged PRs ({len(prs)} total)\n"]
    total = 0

    for pr in prs:
        section: list[str] = []
        section.append(f"### PR #{pr['number']}: {pr['title']}")
        section.append(f"- **Author**: {pr.get('author', '?')}")
        section.append(f"- **Merged**: {pr.get('merged_at', '?')}")
        section.append(f"- **Branch**: {pr.get('branch', '?')}")
        section.append(f"- **Files changed**: {pr.get('files_changed', '?')}")
        additions = pr.get("additions", 0)
        deletions = pr.get("deletions", 0)
        if additions or deletions:
            section.append(f"- **Lines of code**: +{additions} / -{deletions}")
        section.append(f"- **Review decision**: {pr.get('review_decision', 'none')}")

        file_paths = pr.get("file_paths", [])
        if file_paths:
            shown = file_paths[:15]
            section.append(f"- **Changed files** ({len(file_paths)}):")
            for fp in shown:
                section.append(f"  - `{fp}`")
            if len(file_paths) > 15:
                section.append(f"  - ...and {len(file_paths) - 15} more")

        linked = pr.get("linked_issue")
        if linked:
            issue_line = f"- **Linked issue**: {linked.get('key', '')} ({linked.get('source', '')})"
            details = linked.get("details")
            if details:
                issue_line += f" — {details.get('title', '')}"
                ac = details.get("acceptance_criteria", "")
                if ac:
                    section.append(issue_line)
                    section.append(f"  - **Acceptance criteria**: {ac[:500]}")
                else:
                    section.append(issue_line)
                    section.append("  - **Acceptance criteria**: _(none found)_")
            else:
                section.append(issue_line)

        # Human comments summary
        human = pr.get("human_comments", [])
        if human:
            section.append(f"- **Human comments** ({len(human)}):")
            for c in human[:5]:
                body = c.get("body", "")[:300]
                ctype = c.get("type", "comment")
                path = c.get("path", "")
                prefix = f"[{ctype}]" if ctype != "comment" else ""
                file_ref = f" on `{path}`" if path else ""
                section.append(f"  - @{c.get('author', '?')}{file_ref} {prefix}: {body}")
            if len(human) > 5:
                section.append(f"  - ...and {len(human) - 5} more")

        # Bot comments with content (not just count)
        bot = pr.get("bot_comments", [])
        if bot:
            section.append(f"- **Bot comments** ({len(bot)}):")
            for c in bot[:3]:
                body = c.get("body", "")[:300]
                section.append(f"  - @{c.get('author', '?')}: {body}")
            if len(bot) > 3:
                section.append(f"  - ...and {len(bot) - 3} more")

        # PR body excerpt
        body = pr.get("body", "")
        if body:
            excerpt = body[:300].replace("\n", " ")
            section.append(f"- **Description**: {excerpt}")

        pr_text = "\n".join(section) + "\n"
        if len(pr_text) > per_pr_budget:
            pr_text = pr_text[:per_pr_budget] + "\n_(truncated)_\n"

        if total + len(pr_text) > max_chars:
            lines.append(f"\n_(remaining {len(prs) - len(lines) + 1} PRs omitted for token budget)_")
            break
        lines.append(pr_text)
        total += len(pr_text)

    return "\n".join(lines)
