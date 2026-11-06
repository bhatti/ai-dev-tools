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
from urllib.parse import quote

from scripts.analyze import reviewer_scope
from scripts.common.comment_select import select_comments
from scripts.common.config import worker_count
from scripts.common.http_retry import Retryable, call_with_retry
from scripts.common.jira_api import resolve_field_id, fetch_board_issue_keys, search_issues as _jira_search_issues
from scripts.common.issue_fetcher import get_jira_linked_prs
from scripts.common.pr_classify import enrich_pr_with_metrics as _enrich_pr_with_metrics
from scripts.common.pr_metadata import _size_bucket as _size_bucket_fn

from scripts.common.actors import classify_actor, is_ai_authored
from scripts.common.bitbucket_api import api_base

# Rubber-stamp phrases: short approvals with no substantive feedback
_RUBBER_STAMP_RE = re.compile(
    r'^(?:lgtm|looks?\s+good(?:\s+to\s+me)?|l\.?g\.?t\.?m\.?|approved?|ship\s+it|'
    r'nice(?:\s+work)?|good\s+(?:stuff|job|work|one)|'
    r'\+1|👍|💯|🚀|✅|:[\+]?1:|:thumbsup:|:ok(?:_hand)?:|:fire:|:white_check_mark:|'
    r'sounds?\s+good|all\s+good|great(?:\s+work)?|excellent|perfect|awesome|wonderful|'
    r'merge(?:\s+it)?|go(?:\s+for\s+it)?|no\s+(?:issues?|comments?|concerns?|objections?))?'
    r'(?:\s*[!.]*)?$',
    re.IGNORECASE,
)

# build_pr_context: chars of PR metadata reserved per PR before comments get the rest of its budget.
_PR_HEADER_RESERVE = 1200
_MIN_COMMENT_BUDGET = 600

# Cache for raw issue API responses (populated by fetch_issue_details)
_issue_raw_cache: dict[str, dict] = {}


def _reports_dir() -> Path:
    """Return workspace/reports directory, creating it if needed."""
    d = Path(os.environ.get("WORKSPACE_DIR", ".")) / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_raw(filename: str, data: object) -> None:
    """Silently write data as JSON to reports/<filename>."""
    try:
        path = _reports_dir() / filename
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    except Exception as e:
        print(f"[pr-fetch] WARNING: could not write {filename}: {e}", file=sys.stderr, flush=True)


def write_issues_raw() -> None:
    """Write accumulated raw issue API responses to reports/issues_raw.json."""
    if _issue_raw_cache:
        _write_raw("issues_raw.json", _issue_raw_cache)


def size_bucket(additions: int, deletions: int) -> str:
    """Classify PR size into a standard bucket based on total lines changed."""
    return _size_bucket_fn(additions + deletions)


def compute_pr_state_summary(prs: list[dict]) -> dict:
    """Return counts of PRs by state: merged, open, declined, total."""
    counts: dict[str, int] = {"merged": 0, "open": 0, "declined": 0, "total": len(prs)}
    for pr in prs:
        state = (pr.get("state") or "").lower()
        if state in ("merged", "open", "declined"):
            counts[state] += 1
        elif state == "closed":
            counts["declined"] += 1
    return counts


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def fetch_prs(config: dict, n_prs: int = 50, max_bytes: int = 10_000_000, states: list[str] | None = None) -> list[dict]:
    """Dispatch to GitHub or Bitbucket based on DEFAULT_TRACKER.

    states: list of PR states to include. Default None → ["merged", "declined"].
    Pass ["merged", "open", "declined"] for --state all.
    """
    tracker = (config.get("DEFAULT_TRACKER") or "").lower().strip()
    if tracker in ("jira", "jira/bitbucket", "bitbucket"):
        return fetch_bitbucket_prs(config, n_prs, states=states)
    return fetch_github_prs(config, n_prs, states=states)


def fetch_single_pr(config: dict, pr_number: int) -> dict | None:
    """Fetch a single PR by number. Backward-compat wrapper around fetch_prs_by_numbers."""
    prs = fetch_prs_by_numbers(config, [pr_number])
    return prs[0] if prs else None


def parse_pr_url(url: str) -> tuple[str, int]:
    """Parse a GitHub or Bitbucket PR URL and return (tracker_type, pr_number).

    tracker_type is 'github' or 'jira/bitbucket'.
    Raises ValueError for unrecognized URLs.
    """
    url = url.strip().rstrip("/")
    # GitHub: https://github.com/org/repo/pull/123
    m = re.search(r"github\.com/.+/pull/(\d+)", url)
    if m:
        return "github", int(m.group(1))
    # Bitbucket: https://bitbucket.org/workspace/repo/pull-requests/123
    m = re.search(r"bitbucket\.org/.+/pull-requests/(\d+)", url)
    if m:
        return "jira/bitbucket", int(m.group(1))
    raise ValueError(f"Unrecognized PR URL format: {url!r}")


def fetch_prs_by_numbers(config: dict, pr_numbers: list[int]) -> list[dict]:
    """Fetch specific PRs by number. Returns normalized list (same schema as fetch_prs).

    Dispatches to GitHub or Bitbucket based on DEFAULT_TRACKER in config.
    Skips numbers where the API returns None (not found or error).
    """
    tracker = (config.get("DEFAULT_TRACKER") or "").lower().strip()
    results: list[dict] = []
    for num in pr_numbers:
        if tracker in ("jira", "jira/bitbucket", "bitbucket"):
            pr = _fetch_single_bb_pr(config, num)
        else:
            pr = _fetch_single_gh_pr(config, num)
        if pr is not None:
            results.append(pr)
    return results


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

def fetch_github_prs(config: dict, n_prs: int = 50, states: list[str] | None = None) -> list[dict]:
    """Fetch last N PRs via ``gh pr list``.

    states: list of PR states to include. Default None → ["merged", "declined"].
    Pass ["merged", "open", "declined"] for --state all.

    Enriches each PR with inline review comments via the GitHub API.
    Supports optional filtering:
      PR_AUDIT_GH_MILESTONE — scope to a GitHub milestone (sprint equivalent)
      PR_AUDIT_TEAM_MEMBERS — post-filter to PRs authored/reviewed by comma-sep logins
    """
    if states is None:
        states = ["merged", "declined"]
    states_set = set(states)

    org = config.get("GH_ORG", "").strip()
    repo = config.get("GH_REPO", "").strip()
    if not org or not repo:
        print("[pr-fetch] GH_ORG/GH_REPO not set — cannot fetch GitHub PRs", file=sys.stderr, flush=True)
        return []

    fields = (
        "number,title,state,body,author,createdAt,mergedAt,url,headRefName,"
        "comments,reviews,reviewDecision,labels,files,baseRefName"
    )
    has_jira_filter = any([
        config.get("JIRA_BOARDS", ""),
        config.get("JIRA_SPACE", ""),
        config.get("PR_AUDIT_FILTER", ""),
    ])
    scope = resolve_reviewer_scope(config, "github")
    fetch_limit = n_prs * (10 if has_jira_filter else 1)
    milestone = config.get("PR_AUDIT_GH_MILESTONE", "").strip()

    def _gh_list(gh_state: str, search: str = "") -> list[dict]:
        cmd = [
            "gh", "pr", "list",
            "-R", f"{org}/{repo}",
            "--state", gh_state,
            "--limit", str(fetch_limit),
            "--json", fields,
        ]
        if milestone:
            cmd += ["--milestone", milestone]
        if search:
            cmd += ["--search", search]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            print(f"[pr-fetch] gh pr list --state {gh_state} failed: {e}", file=sys.stderr, flush=True)
            return []
        if result.returncode != 0:
            print(f"[pr-fetch] gh pr list --state {gh_state} error: {result.stderr.strip()[:300]}", file=sys.stderr, flush=True)
            return []
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            print(f"[pr-fetch] could not parse gh output: {e}", file=sys.stderr, flush=True)
            return []

    if milestone:
        print(f"[pr-fetch] filtering by milestone: {milestone}", flush=True)
    print(f"[pr-fetch] fetching GH PRs states={states}", flush=True)

    # With --reviewers the tracker narrows the list (search qualifiers), so only matching PRs are enriched. One group
    # per requested name, so a small --n-prs still covers every name (the groups are interleaved below).
    groups: list[reviewer_scope.Scope] = [{name: people} for name, people in scope.items()] or [{}]
    skipped_logins: list[str] = []

    def _collect(searches: list[str]) -> list[dict]:
        rows: dict[int, dict] = {}

        def add(gh_state: str, keep=lambda rp: True) -> None:
            for search in searches:
                for rp in _gh_list(gh_state, search):
                    num = rp.get("number") or 0
                    if num and num not in rows and keep(rp):
                        rows[num] = rp

        if states_set & {"merged", "declined"}:
            add("merged")
            # gh --state closed gives CLOSED PRs; declined = closed without mergedAt
            if "declined" in states_set:
                add("closed", keep=lambda rp: not rp.get("mergedAt"))
        if "open" in states_set:
            add("open")
        return list(rows.values())

    per_group: list[list[dict]] = []
    for group in groups:
        if group:
            searches, skipped = reviewer_scope.gh_searches(group)
            skipped_logins += skipped
        else:
            searches = [""]
        rows = _collect(searches)
        per_group.append(sorted(rows, key=lambda rp: -rp["number"]) if group else rows)
    if skipped_logins:
        print(f"[pr-fetch] GitHub reviewers must be logins; skipped: {', '.join(skipped_logins)}", file=sys.stderr, flush=True)

    raw_prs = _interleave(per_group, "number")
    if scope and not has_jira_filter:
        raw_prs = raw_prs[:n_prs]

    # A.1: save raw PR list to artifact
    _write_raw("pr_data_raw.json", raw_prs)

    prs: list[dict] = []
    reviews_raw: dict[int, list] = {}

    # Pre-fetch inline review comments in parallel
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time as _time

    max_workers = worker_count()
    print(f"[pr-fetch] fetching inline comments for {len(raw_prs)} GH PRs (workers={max_workers})...", flush=True)
    t0 = _time.monotonic()

    inline_by_pr: dict[int, list] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_gh_review_comments, org, repo, rp.get("number", 0)): rp.get("number", 0) for rp in raw_prs}
        for future in as_completed(futures):
            pr_num = futures[future]
            try:
                inline_by_pr[pr_num] = future.result()
            except Exception as exc:
                print(f"[pr-fetch] warn: GH inline comments failed PR#{pr_num}: {exc}", flush=True)
                inline_by_pr[pr_num] = []

    print(f"[pr-fetch] parallel fetch complete: {len(raw_prs)} PRs in {_time.monotonic() - t0:.1f}s", flush=True)

    for rp in raw_prs:
        all_comments: list[dict] = []
        for c in rp.get("comments", []):
            all_comments.append({
                "author": c.get("author", {}).get("login", ""),
                "author_kind": "Bot" if c.get("author", {}).get("is_bot") else "",
                "body": c.get("body", ""),
                "type": "comment",
            })
        for rv in rp.get("reviews", []):
            body = rv.get("body", "").strip()
            if body:
                all_comments.append({
                    "author": rv.get("author", {}).get("login", ""),
                    "author_kind": "Bot" if rv.get("author", {}).get("is_bot") else "",
                    "body": body,
                    "type": "review",
                })

        pr_number = rp.get("number", 0)
        inline = inline_by_pr.get(pr_number, [])
        all_comments.extend(inline)
        if inline:
            reviews_raw[pr_number] = inline

        classified = classify_comments(all_comments)

        files_list = rp.get("files", []) or []
        files_changed = len(files_list) if isinstance(files_list, list) else 0
        additions = sum(f.get("additions", 0) for f in files_list if isinstance(f, dict))
        deletions = sum(f.get("deletions", 0) for f in files_list if isinstance(f, dict))
        file_paths = [f.get("path", "") for f in files_list if isinstance(f, dict)]

        # A.2: derive review_decision from actual reviews array; fall back to reviewDecision field
        reviews_list = rp.get("reviews", [])
        review_decision = _derive_review_decision(reviews_list, rp.get("reviewDecision", ""))

        author = rp.get("author", {}).get("login", "")
        # Approvers: GitHub doesn't expose a simple list, derive from reviews
        gh_approvers = [
            rv.get("author", {}).get("login", "")
            for rv in reviews_list
            if rv.get("state") == "APPROVED" and rv.get("author", {}).get("login")
        ]
        # All reviewers: anyone who submitted any review (approved, requested changes, or commented)
        gh_reviewers = list({
            rv.get("author", {}).get("login", "")
            for rv in reviews_list
            if rv.get("author", {}).get("login")
        } - {author})
        depth = _compute_review_depth(classified["human_comments"], gh_approvers)

        gh_state = (rp.get("state") or "").upper()
        is_merged = bool(rp.get("mergedAt"))
        derived_state = "merged" if is_merged else ("declined" if gh_state == "CLOSED" else "open")

        pr = {
            "number": pr_number,
            "title": rp.get("title", ""),
            "author": author,
            "state": derived_state,
            "created_at": rp.get("createdAt", ""),
            "merged_at": rp.get("mergedAt", ""),
            "url": rp.get("url", ""),
            "branch": rp.get("headRefName", ""),
            "baseRefName": rp.get("baseRefName", ""),
            "labels": [lb.get("name", "") for lb in rp.get("labels") or [] if isinstance(lb, dict)],
            "body": rp.get("body", ""),
            "files_changed": files_changed,
            "additions": additions,
            "deletions": deletions,
            "size_bucket": size_bucket(additions, deletions),
            "file_paths": file_paths[:50],
            "all_comments": all_comments,
            "bot_comments": classified["bot_comments"],
            "ci_comments": classified["ci_comments"],
            "review_bot_comments": classified["review_bot_comments"],
            "human_comments": classified["human_comments"],
            "approvers": gh_approvers,
            "reviewers": gh_reviewers,
            "linked_issue": None,
            "review_decision": review_decision,
            "review_decision_protected": rp.get("reviewDecision", ""),
            "is_bot_authored": is_ai_authored(author),
            **depth,
        }
        classify_files = [
            {"path": f.get("path", ""), "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)}
            for f in files_list if isinstance(f, dict)
        ]
        _enrich_pr_with_metrics(pr, files=classify_files or None)
        pr["file_changes"] = classify_files[:MAX_FILE_CHANGES]   # every changed file with its size: the changelog classifies them all
        pr["files_truncated"] = len(files_list) >= _GH_FILES_LIMIT  # `gh pr list --json files` stops at ~100 files
        prs.append(pr)

    # A.1: save inline review comments to artifact
    if reviews_raw:
        _write_raw("pr_reviews_raw.json", reviews_raw)

    # Post-filter by team members if configured
    prs = _filter_by_team(prs, config.get("PR_AUDIT_TEAM_MEMBERS", ""), tracker="github")

    # Jira-issue-first filter (hybrid GH+Jira setups: --board / --team <name>)
    resolved = _resolve_jira_issue_keys(config)
    if resolved is not None:
        issue_keys, jira_issues = resolved
        config["_jira_issues_reviewed"] = len(issue_keys)
        prs = _filter_prs_by_issue_keys(prs, issue_keys)
        prs = _supplement_from_dev_status(config, jira_issues, prs)
    else:
        # GH-native: label filter from --filter label=X or --team <name> as label
        pr_filter = config.get("PR_AUDIT_FILTER", "").strip()
        if pr_filter and pr_filter.lower().startswith("label="):
            prs = _filter_by_label(prs, pr_filter.split("=", 1)[1].strip())
        elif config.get("JIRA_SPACE", "").strip() and config.get("DEFAULT_TRACKER", "").lower() == "github":
            prs = _filter_by_label(prs, config["JIRA_SPACE"].strip())

    # Last on purpose: the Jira dev-status supplement above adds PRs fetched without any reviewer filter.
    if scope and has_jira_filter:
        print("[pr-fetch] reviewers filter is applied on top of the Jira scope (JIRA_BOARDS / JIRA_SPACE / --filter)", flush=True)
    prs = reviewer_scope.filter_prs(prs, scope)[:n_prs]
    print(f"[pr-fetch] fetched {len(prs)} merged PRs from GitHub ({org}/{repo})", flush=True)
    return prs


def _derive_review_decision(reviews: list, fallback: str = "") -> str:
    """Derive review decision from the reviews array (more reliable than reviewDecision field).

    Falls back to the reviewDecision field value when the reviews array is empty,
    since some repos populate reviewDecision via branch protection rules.
    """
    states = [r.get("state", "") for r in (reviews or [])]
    if "CHANGES_REQUESTED" in states:
        return "CHANGES_REQUESTED"
    if "APPROVED" in states:
        return "APPROVED"
    if states:
        return "COMMENTED"
    return fallback or ""


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
            "author_kind": c.get("user", {}).get("type", ""),
            "body": c.get("body", ""),
            "type": "review_comment",
            "path": c.get("path", ""),
            "diff_hunk": c.get("diff_hunk", "")[:200],
        })
    return comments


def _fetch_single_gh_pr(config: dict, pr_number: int) -> dict | None:
    """Fetch a single GH PR by number, reusing the same helpers as fetch_github_prs."""
    org = config.get("GH_ORG", "").strip()
    repo = config.get("GH_REPO", "").strip()
    if not org or not repo:
        print("[pr-fetch] GH_ORG/GH_REPO not set — cannot fetch single GH PR", file=sys.stderr, flush=True)
        return None

    fields = "number,title,body,author,createdAt,mergedAt,state,url,headRefName,baseRefName,comments,reviews,reviewDecision,labels,files"
    cmd = ["gh", "pr", "view", str(pr_number), "-R", f"{org}/{repo}", "--json", fields]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"[pr-fetch] gh pr view failed: {e}", file=sys.stderr, flush=True)
        return None
    if result.returncode != 0:
        print(f"[pr-fetch] gh pr view error: {result.stderr.strip()[:300]}", file=sys.stderr, flush=True)
        return None
    try:
        rp = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        print(f"[pr-fetch] could not parse gh pr view output: {e}", file=sys.stderr, flush=True)
        return None

    all_comments: list[dict] = []
    for c in rp.get("comments", []):
        all_comments.append({
            "author": c.get("author", {}).get("login", ""),
            "author_kind": "Bot" if c.get("author", {}).get("is_bot") else "",
            "body": c.get("body", ""),
            "type": "comment",
        })
    for rv in rp.get("reviews", []):
        body = rv.get("body", "").strip()
        if body:
            all_comments.append({
                "author": rv.get("author", {}).get("login", ""),
                "author_kind": "Bot" if rv.get("author", {}).get("is_bot") else "",
                "body": body,
                "type": "review",
            })
    inline = _fetch_gh_review_comments(org, repo, pr_number)
    all_comments.extend(inline)

    classified = classify_comments(all_comments)
    files_list = rp.get("files", []) or []
    files_changed = len(files_list) if isinstance(files_list, list) else 0
    additions = sum(f.get("additions", 0) for f in files_list if isinstance(f, dict))
    deletions = sum(f.get("deletions", 0) for f in files_list if isinstance(f, dict))
    file_paths = [f.get("path", "") for f in files_list if isinstance(f, dict)]

    reviews_list = rp.get("reviews", [])
    review_decision = _derive_review_decision(reviews_list, rp.get("reviewDecision", ""))
    author = rp.get("author", {}).get("login", "")
    gh_approvers = [
        rv.get("author", {}).get("login", "")
        for rv in reviews_list
        if rv.get("state") == "APPROVED" and rv.get("author", {}).get("login")
    ]
    depth = _compute_review_depth(classified["human_comments"], gh_approvers)

    gh_state = rp.get("state", "OPEN").upper()
    is_merged = bool(rp.get("mergedAt")) or gh_state == "MERGED"
    state = "merged" if is_merged else ("declined" if gh_state == "CLOSED" else "open")

    return {
        "number": rp.get("number", pr_number),
        "title": rp.get("title", ""),
        "author": author,
        "state": state,
        "created_at": rp.get("createdAt", ""),
        "merged_at": rp.get("mergedAt", ""),
        "url": rp.get("url", ""),
        "branch": rp.get("headRefName", ""),
        "baseRefName": rp.get("baseRefName", ""),
        "labels": [lb.get("name", "") for lb in rp.get("labels") or [] if isinstance(lb, dict)],
        "body": rp.get("body", ""),
        "files_changed": files_changed,
        "additions": additions,
        "deletions": deletions,
        "size_bucket": size_bucket(additions, deletions),
        "file_paths": file_paths[:50],
        "all_comments": all_comments,
        "bot_comments": classified["bot_comments"],
        "ci_comments": classified["ci_comments"],
        "review_bot_comments": classified["review_bot_comments"],
        "human_comments": classified["human_comments"],
        "approvers": gh_approvers,
        "linked_issue": None,
        "review_decision": review_decision,
        "review_decision_protected": rp.get("reviewDecision", ""),
        "is_bot_authored": is_ai_authored(author),
        **depth,
    }


# ---------------------------------------------------------------------------
# Bitbucket
# ---------------------------------------------------------------------------

def fetch_bitbucket_prs(config: dict, n_prs: int = 50, states: list[str] | None = None) -> list[dict]:
    """Fetch last N PRs via Bitbucket REST API.

    states: list of PR states to include. Default None → ["merged", "declined"].
    Pass ["merged", "open", "declined"] for --state all.

    Supports optional filtering:
      JIRA_BOARDS     — filter to PRs referencing issues from these board(s) (Jira-issue-first)
      JIRA_SPACE      — filter to PRs referencing issues tagged with this team (Eng Scrum Team field)
      PR_AUDIT_FILTER — filter by field=value (Jira JQL) or label=X (GitHub-native)
      PR_AUDIT_TEAM_MEMBERS — post-filter to PRs authored/reviewed by comma-sep display names
    """
    if states is None:
        states = ["merged", "declined"]
    _bb_state_map = {"merged": "MERGED", "open": "OPEN", "declined": "DECLINED"}
    bb_states = [_bb_state_map[s] for s in states if s in _bb_state_map]
    if not bb_states:
        bb_states = ["MERGED"]

    workspace, repo, auth = _bb_credentials(config)
    if not workspace or not repo:
        print("[pr-fetch] BITBUCKET_WORKSPACE/REPO not set", file=sys.stderr, flush=True)
        return []

    base = f"{api_base()}/repositories/{workspace}/{repo}"
    state_params = "&".join(f"state={s}" for s in bb_states)
    # +values.participants adds approval data without removing any default fields
    url = f"{base}/pullrequests?{state_params}&sort=-updated_on&pagelen=50&fields=%2Bvalues.participants"
    print(f"[pr-fetch] fetching BB PRs states={bb_states}", flush=True)

    # Over-fetch when any Jira filter is active so there are enough PRs after filtering.
    has_filter = any([
        config.get("JIRA_BOARDS", ""),
        config.get("JIRA_SPACE", ""),
        config.get("PR_AUDIT_FILTER", ""),
    ])
    scope = resolve_reviewer_scope(config, "bitbucket")
    fetch_cap = n_prs * (10 if has_filter else 1)

    if scope:
        # Server-side BBQL on assigned reviewers first, one query per requested name so a small --n-prs still covers
        # every name; commenters who were never assigned cannot be filtered server-side, so only if that is not
        # enough scan PRs that have comments and match on participants.
        per_name = [_bb_list_prs(f"{url}&q={quote(reviewer_scope.bbql({name: people}), safe='')}", auth, fetch_cap)
                    for name, people in scope.items()]
        raw_prs = _interleave(per_name, "id")
        print(f"[pr-fetch] reviewers: assigned-reviewer queries matched {len(raw_prs)} PRs "
              f"({', '.join(f'{n}: {len(r)}' for n, r in zip(scope, per_name))})", flush=True)
        if len(raw_prs) < n_prs:
            seen = {rp.get("id") for rp in raw_prs}
            scan = _bb_list_prs(f"{url}&q={quote('comment_count>0', safe='')}", auth, min(n_prs * _REVIEWER_OVERSCAN, _REVIEWER_SCAN_CAP))
            found = [rp for rp in reviewer_scope.prefilter_bb_rows(scan, scope) if rp.get("id") not in seen]
            raw_prs += found
            print(f"[pr-fetch] reviewers: scanned {len(scan)} commented PRs, {len(found)} involve them", flush=True)
        if not has_filter:
            raw_prs = raw_prs[:n_prs]
    else:
        raw_prs = _bb_list_prs(url, auth, fetch_cap)

    # A.1: save raw PR list to artifact
    _write_raw("pr_data_raw.json", raw_prs[:fetch_cap])

    prs: list[dict] = []
    reviews_raw: dict[int, list] = {}

    # Parallel fetch: comments + diffstat per PR
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import time as _time

    max_workers = worker_count()
    to_fetch = raw_prs[:fetch_cap]
    print(f"[pr-fetch] fetching comments+diffstat for {len(to_fetch)} BB PRs (workers={max_workers})...", flush=True)
    t0 = _time.monotonic()

    def _fetch_bb_pr_details(rp: dict) -> tuple[dict, list, list]:
        pr_id = rp.get("id", 0)
        comments = _fetch_bb_comments(base, pr_id, auth)
        diffstat = _fetch_bb_diffstat(base, pr_id, auth)
        return rp, comments, diffstat

    fetched: list[tuple[dict, list, list]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_bb_pr_details, rp): rp.get("id", 0) for rp in to_fetch}
        for future in as_completed(futures):
            try:
                fetched.append(future.result())
            except Exception as exc:
                pr_id = futures[future]
                print(f"[pr-fetch] warn: BB PR#{pr_id} fetch failed: {exc}", flush=True)

    print(f"[pr-fetch] parallel fetch complete: {len(fetched)} PRs in {_time.monotonic() - t0:.1f}s", flush=True)

    _bb_state_to_str = {"MERGED": "merged", "OPEN": "open", "DECLINED": "declined", "SUPERSEDED": "declined"}

    for rp, all_comments, diffstat in fetched:
        pr_id = rp.get("id", 0)
        if all_comments:
            reviews_raw[pr_id] = all_comments
        classified = classify_comments(all_comments)

        files_changed = len(diffstat)
        additions = sum(d.get("lines_added", 0) for d in diffstat)
        deletions = sum(d.get("lines_removed", 0) for d in diffstat)
        file_paths = [d.get("path", "") for d in diffstat]

        participants = rp.get("participants", [])
        approvers = [
            p.get("user", {}).get("display_name") or p.get("user", {}).get("nickname", "")
            for p in participants
            if p.get("approved") and p.get("role") != "AUTHOR"
        ]
        approvers = [a for a in approvers if a]
        assigned_reviewers = [
            p.get("user", {}).get("display_name") or p.get("user", {}).get("nickname", "")
            for p in participants
            if p.get("role") == "REVIEWER"
        ]
        assigned_reviewers = [r for r in assigned_reviewers if r]
        review_decision = "APPROVED" if approvers else ""

        author = rp.get("author", {}).get("display_name", rp.get("author", {}).get("nickname", ""))
        depth = _compute_review_depth(classified["human_comments"], approvers)

        bb_state_raw = (rp.get("state", "MERGED")).upper()
        pr_state_str = _bb_state_to_str.get(bb_state_raw, "merged")
        pr = {
            "number": pr_id,
            "title": rp.get("title", ""),
            "author": author,
            "state": pr_state_str,
            "created_at": rp.get("created_on", ""),
            "merged_at": rp.get("updated_on", "") if pr_state_str == "merged" else "",
            "url": rp.get("links", {}).get("html", {}).get("href", ""),
            "branch": rp.get("source", {}).get("branch", {}).get("name", ""),
            "headRefName": rp.get("source", {}).get("branch", {}).get("name", ""),
            "baseRefName": rp.get("destination", {}).get("branch", {}).get("name", "main"),
            "body": rp.get("description", ""),
            "files_changed": files_changed,
            "additions": additions,
            "deletions": deletions,
            "size_bucket": size_bucket(additions, deletions),
            "file_paths": file_paths[:50],
            "all_comments": all_comments,
            "bot_comments": classified["bot_comments"],
            "ci_comments": classified["ci_comments"],
            "review_bot_comments": classified["review_bot_comments"],
            "human_comments": classified["human_comments"],
            "approvers": approvers,
            "reviewers": assigned_reviewers,
            "participant_groups": [reviewer_scope.person_ids(p.get("user") or {}) for p in participants if p.get("role") != "AUTHOR"],
            "linked_issue": None,
            "review_decision": review_decision,
            "is_bot_authored": is_ai_authored(author),
            **depth,
        }
        classify_files = [
            {"path": d.get("path", ""), "additions": d.get("lines_added", 0), "deletions": d.get("lines_removed", 0)}
            for d in diffstat
        ]
        _enrich_pr_with_metrics(pr, files=classify_files or None)
        pr["file_changes"] = classify_files[:MAX_FILE_CHANGES]
        pr["files_truncated"] = False
        prs.append(pr)

    # A.1: save per-PR comments to artifact
    if reviews_raw:
        _write_raw("pr_reviews_raw.json", reviews_raw)

    # Name-based member filter (--team alice,bob → PR_AUDIT_TEAM_MEMBERS)
    team_str = config.get("PR_AUDIT_TEAM_MEMBERS", "").strip()
    if team_str:
        prs = _filter_by_team(prs, team_str, tracker="bitbucket")

    # Jira-issue-first filter: --board / --team <name> / --filter field=value
    resolved = _resolve_jira_issue_keys(config)
    if resolved is not None:
        issue_keys, jira_issues = resolved
        config["_jira_issues_reviewed"] = len(issue_keys)
        prs = _filter_prs_by_issue_keys(prs, issue_keys)
        prs = _supplement_from_dev_status(config, jira_issues, prs)

    # Last on purpose: the Jira dev-status supplement above adds PRs fetched without any reviewer filter.
    if scope and has_filter:
        print("[pr-fetch] reviewers filter is applied on top of the Jira scope (JIRA_BOARDS / JIRA_SPACE / --filter)", flush=True)
    prs = reviewer_scope.filter_prs(prs, scope)[:n_prs]
    print(f"[pr-fetch] fetched {len(prs)} merged PRs from Bitbucket ({workspace}/{repo})", flush=True)
    return prs


def _interleave(groups: list[list[dict]], key: str) -> list[dict]:
    """Round-robin over ``groups`` (newest first within each), each row once: with a cap, every group is represented."""
    seen: set = set()
    out: list[dict] = []
    for i in range(max((len(g) for g in groups), default=0)):
        for g in groups:
            if i < len(g) and g[i].get(key) not in seen:
                seen.add(g[i].get(key))
                out.append(g[i])
    return out


def _bb_get_page(url: str, auth: tuple | None) -> dict:
    """One Bitbucket list page; 429 and 5xx are retried with backoff (Retry-After honored)."""
    import requests as _requests

    def attempt() -> dict:
        try:
            resp = _requests.get(url, auth=auth, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except _requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else 0
            if status == 429 or status >= 500:
                try:
                    delay = float(e.response.headers.get("Retry-After", ""))
                except (TypeError, ValueError):
                    delay = None
                raise Retryable(f"Bitbucket HTTP {status}", delay) from e
            raise
        except (_requests.ConnectionError, _requests.Timeout) as e:
            raise Retryable(f"Bitbucket request failed: {type(e).__name__}") from e

    return call_with_retry(attempt)


def _bb_list_prs(url: str, auth: tuple | None, cap: int) -> list[dict]:
    """Pages of a Bitbucket pull-request list until ``cap`` rows; an API error ends the list with what was read."""
    rows: list[dict] = []
    seen: set = set()
    while url and len(rows) < cap:
        try:
            data = _bb_get_page(url, auth)
        except Exception as e:
            print(f"[pr-fetch] Bitbucket API error: {e}", file=sys.stderr, flush=True)
            break
        for rp in data.get("values", []):
            # A query on an array field (reviewers.*) returns a PR once per matching element: keep it once.
            if rp.get("id") is None or rp["id"] not in seen:
                seen.add(rp.get("id"))
                rows.append(rp)
        url = data.get("next", "")
    return rows


def _fetch_bb_comments(base_url: str, pr_id: int, auth: tuple | None) -> list[dict]:
    """All comments of a Bitbucket PR (every page, deleted ones skipped). A failed page ends the list; what was read is kept."""
    import requests as _requests

    url: str | None = f"{base_url}/pullrequests/{pr_id}/comments?pagelen=100"
    comments: list[dict] = []
    for _ in range(50):  # same page cap as bitbucket_api.list_pr_comments
        if not url:
            break
        try:
            resp = _requests.get(url, auth=auth, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            print(f"[pr-fetch] warn: Bitbucket comments for PR {pr_id} cut short ({type(e).__name__}); counts may be low", file=sys.stderr, flush=True)
            break
        for c in data.get("values", []):
            if c.get("deleted"):
                continue
            user = c.get("user", {})
            comments.append({
                "author": user.get("display_name", user.get("nickname", "")),
                "author_alias": user.get("nickname", ""),
                "author_id": user.get("account_id") or user.get("uuid", ""),
                "author_kind": user.get("account_type", ""),
                "body": c.get("content", {}).get("raw", ""),
                "type": "comment",
            })
        url = data.get("next")
    return comments


def _fetch_bb_diffstat(base_url: str, pr_id: int, auth: tuple | None) -> list[dict]:
    """Diffstat (file-level additions/deletions) for a Bitbucket PR, following pagination (100 files per page)."""
    url = f"{base_url}/pullrequests/{pr_id}/diffstat?pagelen=100"
    files: list[dict] = []
    for _ in range(_BB_DIFFSTAT_PAGES):
        if not url:
            break
        try:
            data = _bb_get_page(url, auth)
        except Exception as e:
            print(f"[pr-fetch] warn: Bitbucket diffstat for PR {pr_id} cut short ({type(e).__name__}); file list may be incomplete", file=sys.stderr, flush=True)
            break
        for entry in data.get("values", []):
            new_file = entry.get("new", {}) or {}
            old_file = entry.get("old", {}) or {}
            files.append({
                "path": new_file.get("path", "") or old_file.get("path", ""),
                "lines_added": entry.get("lines_added") or 0,  # null for binary files
                "lines_removed": entry.get("lines_removed") or 0,
            })
        url = data.get("next", "")
    return files


def _fetch_single_bb_pr(config: dict, pr_id: int) -> dict | None:
    """Fetch a single BB PR by ID, reusing the same helpers as fetch_bitbucket_prs."""
    import requests as _requests

    workspace = config.get("BITBUCKET_WORKSPACE", "").strip()
    repo = config.get("BITBUCKET_REPO", "").strip()
    if not workspace or not repo:
        print("[pr-fetch] BITBUCKET_WORKSPACE/REPO not set — cannot fetch single PR", file=sys.stderr, flush=True)
        return None

    username = config.get("BITBUCKET_USERNAME", "").strip()
    token = config.get("BITBUCKET_TOKEN", config.get("BITBUCKET_APP_PASSWORD", "")).strip()
    auth = (username, token) if username and token else None

    base = f"{api_base()}/repositories/{workspace}/{repo}"
    # Single-PR endpoint: %2Bparticipants (not %2Bvalues.participants which is for the list endpoint)
    url = f"{base}/pullrequests/{pr_id}?fields=%2Bparticipants"
    try:
        resp = _requests.get(url, auth=auth, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"[pr-fetch] Bitbucket single-PR API error: {e}", file=sys.stderr, flush=True)
        return None
    rp = resp.json()

    all_comments = _fetch_bb_comments(base, pr_id, auth)
    classified = classify_comments(all_comments)

    diffstat = _fetch_bb_diffstat(base, pr_id, auth)
    files_changed = len(diffstat)
    additions = sum(d.get("lines_added", 0) for d in diffstat)
    deletions = sum(d.get("lines_removed", 0) for d in diffstat)
    file_paths = [d.get("path", "") for d in diffstat]

    participants = rp.get("participants", [])
    approvers = [
        p.get("user", {}).get("display_name") or p.get("user", {}).get("nickname", "")
        for p in participants
        if p.get("approved") and p.get("role") != "AUTHOR"
    ]
    approvers = [a for a in approvers if a]
    review_decision = "APPROVED" if approvers else ""

    author = rp.get("author", {}).get("display_name", rp.get("author", {}).get("nickname", ""))
    depth = _compute_review_depth(classified["human_comments"], approvers)

    bb_state = rp.get("state", "OPEN").upper()
    is_merged = bb_state == "MERGED"
    state = "merged" if is_merged else ("declined" if bb_state in ("DECLINED", "SUPERSEDED") else "open")

    return {
        "number": rp.get("id", pr_id),
        "title": rp.get("title", ""),
        "author": author,
        "state": state,
        "created_at": rp.get("created_on", ""),
        "merged_at": rp.get("updated_on", "") if is_merged else "",
        "url": rp.get("links", {}).get("html", {}).get("href", ""),
        "branch": rp.get("source", {}).get("branch", {}).get("name", ""),
        "headRefName": rp.get("source", {}).get("branch", {}).get("name", ""),
        "baseRefName": rp.get("destination", {}).get("branch", {}).get("name", "main"),
        "body": rp.get("description", ""),
        "files_changed": files_changed,
        "additions": additions,
        "deletions": deletions,
        "size_bucket": size_bucket(additions, deletions),
        "file_paths": file_paths[:50],
        "all_comments": all_comments,
        "bot_comments": classified["bot_comments"],
        "ci_comments": classified["ci_comments"],
        "review_bot_comments": classified["review_bot_comments"],
        "human_comments": classified["human_comments"],
        "approvers": approvers,
        "linked_issue": None,
        "review_decision": review_decision,
        "is_bot_authored": is_ai_authored(author),
        **depth,
    }


# ---------------------------------------------------------------------------
# Team filtering helpers
# ---------------------------------------------------------------------------

def _filter_by_team(prs: list[dict], team_str: str, tracker: str = "github") -> list[dict]:
    """Post-filter PRs to those authored or reviewed by team members.

    team_str: comma-separated GitHub logins (tracker=github) or Jira display names (tracker=bitbucket).
    Returns all prs unmodified when team_str is empty.
    """
    if not team_str.strip():
        return prs
    members = {m.strip().lower() for m in team_str.split(",") if m.strip()}
    if not members:
        return prs

    filtered = []
    for pr in prs:
        author = (pr.get("author") or "").lower()
        if author in members:
            filtered.append(pr)
            continue
        # Check all involvement: human commenters + approvers + assigned reviewers
        involved = {
            c.get("author", "").lower()
            for c in pr.get("human_comments", [])
            if c.get("author")
        }
        involved.update(a.lower() for a in pr.get("approvers", []) if a)
        involved.update(r.lower() for r in pr.get("reviewers", []) if r)
        if involved & members:
            filtered.append(pr)

    if len(filtered) != len(prs):
        print(
            f"[pr-fetch] team filter ({tracker}): {len(prs)} → {len(filtered)} PRs "
            f"(members: {', '.join(sorted(members)[:5])}{'...' if len(members) > 5 else ''})",
            flush=True,
        )
    return filtered


# --reviewers: the pure parts (resolving names to people, queries, matching) are in reviewer_scope.py; here are the
# calls that need the tracker. GitHub narrows PRs with search qualifiers, Bitbucket with BBQL on assigned reviewers
# (it cannot filter on commenters, so a bounded scan of commented PRs covers those).
MAX_FILE_CHANGES = 400      # changed files kept per PR (path + additions/deletions)
_GH_FILES_LIMIT = 100       # `gh pr list --json files` returns at most about this many files per PR
_BB_DIFFSTAT_PAGES = 10     # Bitbucket diffstat pages (100 files each) followed per PR
_REVIEWER_OVERSCAN = 10
_REVIEWER_SCAN_CAP = 2000   # commented PRs examined for non-assigned commenters, whatever --n-prs is
_BB_MEMBER_PAGES = 30       # workspace member pages (100 each) read to resolve names
_BB_DIRECTORY_SCAN = 500    # PR rows read for participants when the members API is not available


def _bb_credentials(config: dict) -> tuple[str, str, tuple | None]:
    workspace = config.get("BITBUCKET_WORKSPACE", "").strip()
    repo = config.get("BITBUCKET_REPO", "").strip()
    username = config.get("BITBUCKET_USERNAME", "").strip()
    token = config.get("BITBUCKET_TOKEN", config.get("BITBUCKET_APP_PASSWORD", "")).strip()
    return workspace, repo, (username, token) if username and token else None


def _bb_directory(config: dict) -> list[dict]:
    """Users to resolve names against: the workspace members (complete), else the participants of recent PRs."""
    workspace, repo, auth = _bb_credentials(config)
    fields = "next,values.user.display_name,values.user.nickname,values.user.uuid,values.user.account_id"
    url, users = f"{api_base()}/workspaces/{workspace}/members?pagelen=100&fields={fields}", []
    try:
        for _ in range(_BB_MEMBER_PAGES):
            data = _bb_get_page(url, auth)
            users += [v["user"] for v in data.get("values", []) if v.get("user")]
            url = data.get("next", "")
            if not url:
                break
        return users
    except Exception as e:
        print(f"[pr-fetch] workspace members unavailable ({type(e).__name__}); resolving names from recent PR participants", flush=True)
    rows = _bb_list_prs(f"{api_base()}/repositories/{workspace}/{repo}/pullrequests?state=MERGED&state=DECLINED&sort=-updated_on&pagelen=50"
                        f"&fields=%2Bvalues.participants", auth, _BB_DIRECTORY_SCAN)
    return [p["user"] for rp in rows for p in rp.get("participants") or [] if p.get("user")]


def resolve_reviewer_scope(config: dict, tracker: str) -> reviewer_scope.Scope:
    """Requested ``PR_AUDIT_REVIEWERS`` resolved to people and cached on ``config``. GitHub names are logins and stay
    literal; Bitbucket names resolve against the workspace (name, nickname, initials, id)."""
    names = ",".join(reviewer_scope.reviewer_list(config.get("PR_AUDIT_REVIEWERS", "")))
    if not names:
        return {}
    if "_reviewer_scope" not in config:
        scope = reviewer_scope.resolve(names, _bb_directory(config)) if tracker == "bitbucket" else reviewer_scope.literal_scope(names)
        for name, people in scope.items():
            print(f"[pr-fetch] reviewer {name!r} -> {', '.join(p.get('display_name') or p.get('nickname', '?') for p in people)}", flush=True)
        config["_reviewer_scope"] = scope
    return config["_reviewer_scope"]


def _resolve_jira_issue_keys(config: dict) -> tuple[set[str], list[dict]] | None:
    """Resolve Jira issue keys from board, team, or field filter config.

    Returns (keys, issues) where *keys* is the set of issue keys and *issues*
    is the full list of issue dicts (with ``id``/``key`` for dev-status lookup),
    or None when no filter is configured.
    """
    board_str = config.get("JIRA_BOARDS", "").strip()
    team_name = config.get("JIRA_SPACE", "").strip()
    pr_filter = config.get("PR_AUDIT_FILTER", "").strip()

    if not any([board_str, team_name, pr_filter]):
        return None

    # label=X is a GitHub-native filter — not a Jira issue lookup.
    # Return None (no filter) so the GH label path in the caller handles it.
    if pr_filter.lower().startswith("label=") and not board_str and not team_name:
        return None

    if not all([config.get("JIRA_BASE_URL"), config.get("JIRA_EMAIL"), config.get("JIRA_API_TOKEN")]):
        print("[pr-fetch] JIRA creds not set — skipping issue-key filter", flush=True)
        return None

    keys: set[str] = set()
    all_issues: list[dict] = []

    # Board filter: fetch issues from recent sprints only.
    # SKIP if team_name is configured — JQL team filter (below) is more precise than sprint
    # fetch, which returns hundreds of keys from many teams on shared boards.
    if board_str and not team_name:
        for bid in (b.strip() for b in board_str.split(",") if b.strip()):
            try:
                board_keys = fetch_board_issue_keys(config, bid, max_results=500, days_back=30)
                keys |= board_keys
                print(f"[pr-fetch] board {bid}: {len(board_keys)} issue keys", flush=True)
            except Exception as e:
                print(f"[pr-fetch] WARNING: board {bid} key fetch failed: {e}", flush=True)

    # Team filter: JQL by custom field value (preferred over board sprint fetch when team is known)
    if team_name:
        field_name = config.get("JIRA_TEAM_FIELD", "Eng Scrum Team")
        field_id = resolve_field_id(config, field_name)
        if field_id:
            safe_team = team_name.replace("\\", "\\\\").replace('"', '\\"')
            # Include recently closed issues (merged PR → closed issue) and any updated in last 30d.
            # Time window matches typical PR audit lookback; status filter prefers closed/done
            # but falls back to recently-updated issues to avoid missing in-flight work.
            jql = (
                f'{field_id} = "{safe_team}" AND updated >= -30d '
                f'ORDER BY updated DESC'
            )
            issues = _jira_search_issues(config, jql, max_results=500, fields=["summary"])
            team_keys = {i["key"] for i in issues}
            keys |= team_keys
            all_issues.extend(issues)
            print(f"[pr-fetch] team '{team_name}': {len(team_keys)} issue keys", flush=True)
        else:
            print(f"[pr-fetch] WARNING: Jira field '{field_name}' not found — skipping team filter", flush=True)

    # Explicit field=value filter (non-label)
    if pr_filter and "=" in pr_filter:
        field_name, _, field_value = pr_filter.partition("=")
        field_name, field_value = field_name.strip(), field_value.strip()
        if field_name.lower() != "label":
            field_id = resolve_field_id(config, field_name)
            if field_id:
                safe_value = field_value.replace("\\", "\\\\").replace('"', '\\"')
                jql = f'{field_id} = "{safe_value}" ORDER BY updated DESC'
                issues = _jira_search_issues(config, jql, max_results=500, fields=["summary"])
                filter_keys = {i["key"] for i in issues}
                keys |= filter_keys
                all_issues.extend(issues)
                print(f"[pr-fetch] filter '{field_name}'='{field_value}': {len(filter_keys)} issue keys", flush=True)

    print(f"[pr-fetch] total issue key set: {len(keys)} keys", flush=True)
    return keys, all_issues


def _filter_prs_by_issue_keys(prs: list[dict], issue_keys: set[str]) -> list[dict]:
    """Keep PRs whose title, body, or branch references any key in issue_keys."""
    if not issue_keys:
        print("[pr-fetch] WARNING: issue-key filter active but 0 issue keys resolved — no PRs will match", flush=True)
        return []
    filtered = []
    for pr in prs:
        text = f"{pr.get('title', '')} {pr.get('body', '')} {pr.get('branch', '')}"
        found = set(re.findall(r'[A-Z][A-Z0-9]+-\d+', text))
        if found & issue_keys:
            filtered.append(pr)
    print(f"[pr-fetch] issue-key filter: {len(prs)} → {len(filtered)} PRs", flush=True)
    return filtered


def _filter_by_label(prs: list[dict], label_value: str) -> list[dict]:
    """Keep PRs with a label matching label_value (case-insensitive)."""
    wanted = label_value.lower()
    filtered = [pr for pr in prs
                if wanted in [lb.lower() for lb in pr.get("labels", [])]]
    print(f"[pr-fetch] label filter '{label_value}': {len(prs)} → {len(filtered)} PRs", flush=True)
    return filtered


_PR_URL_NUMBER_RE = re.compile(r'/pull(?:-requests?)?/(\d+)')


def _supplement_from_dev_status(
    config: dict, jira_issues: list[dict], prs: list[dict],
) -> list[dict]:
    """Add PRs found via Jira dev-status API that text matching missed.

    Queries at most 50 issues (most recently updated first) to avoid excessive
    API calls.  Returns *prs* with any newly-fetched PRs appended.
    """
    if not jira_issues:
        return prs
    existing_numbers = {p.get("number") for p in prs}
    extra_nums: list[int] = []
    for issue in jira_issues[:50]:
        linked = get_jira_linked_prs(config, issue.get("key", ""), issue.get("id", ""))
        for lp in linked:
            m = _PR_URL_NUMBER_RE.search(lp.get("url", ""))
            if m:
                pr_num = int(m.group(1))
                if pr_num not in existing_numbers:
                    existing_numbers.add(pr_num)
                    extra_nums.append(pr_num)
    if extra_nums:
        extra_prs = fetch_prs_by_numbers(config, extra_nums)
        prs = prs + extra_prs
        print(f"[pr-fetch] dev-status added {len(extra_prs)} PRs not found via text matching", flush=True)
    return prs


# ---------------------------------------------------------------------------
# Comment classification
# ---------------------------------------------------------------------------

def classify_comments(comments: list[dict]) -> dict:
    """Separate comments into ci_comments, review_bot_comments, and human_comments.

    CI bots (build runners, test reporters) are separated from code-review bots
    (AI reviewers, SAST tools) so the caller can compute meaningful skill catch rates.

    Returns:
        {
            'ci_comments': [...],        # CI/build automation
            'review_bot_comments': [...], # AI code-review bots
            'human_comments': [...],     # humans
            'bot_comments': [...],       # union of ci + review_bot (backward compat)
        }
    """
    ci: list[dict] = []
    review_bot: list[dict] = []
    human: list[dict] = []
    for c in comments:
        author = c.get("author", "")
        bucket = classify_actor(author, c.get("body", ""), c.get("author_kind", ""))
        if bucket == "ci":
            ci.append(c)
        elif bucket == "review_bot":
            review_bot.append(c)
        else:
            human.append(c)
    return {
        "ci_comments": ci,
        "review_bot_comments": review_bot,
        "human_comments": human,
        "bot_comments": ci + review_bot,
    }


def _is_rubber_stamp_comment(body: str) -> bool:
    """Return True if the comment body is a rubber-stamp with no substantive feedback.

    Catches: silent approval, LGTM, +1, looks good, emoji-only, and short generic praise.
    """
    stripped = body.strip()
    if not stripped:
        return True
    # Emoji / unicode-only (no ASCII words)
    if re.match(r'^[\s\U0001F000-\U0001FFFF☀-➿⬀-⯿!-/]+$', stripped):
        return True
    # Short (under 60 chars) phrase that matches known rubber-stamp patterns
    return len(stripped) < 60 and bool(_RUBBER_STAMP_RE.fullmatch(stripped))


def _compute_review_depth(human_comments: list[dict], approvers: list[str]) -> dict:
    """Return review depth metrics for a PR.

    Returns:
        substantive_human_comment_count: human comments that contain real feedback
        rubber_stamp_approvers: approvers who left no substantive comments
        has_substantive_review: True if ≥1 human left a non-rubber-stamp comment
    """
    # Build set of humans who left at least one substantive comment
    substantive_commenters: set[str] = set()
    substantive_count = 0
    for c in human_comments:
        if not _is_rubber_stamp_comment(c.get("body", "")):
            substantive_count += 1
            author = c.get("author", "")
            if author:
                substantive_commenters.add(author)

    # Approvers who left zero substantive comments on this PR
    rubber_stamp = [a for a in approvers if a not in substantive_commenters]

    return {
        "substantive_human_comment_count": substantive_count,
        "rubber_stamp_approvers": rubber_stamp,
        "has_substantive_review": substantive_count > 0,
        "review_depth_known": True,   # the defaults on a PR say "no review"; only this flag says the comments were analysed
    }


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
    has_ac, ac_text = _detect_acceptance_criteria(body)
    # A.1: cache raw issue data
    _issue_raw_cache[issue_ref.get("key", key)] = data
    return {
        "title": data.get("title", ""),
        "body": body,
        "labels": [lb.get("name", "") for lb in data.get("labels", [])],
        "acceptance_criteria": ac_text,
        "has_acceptance_criteria": has_ac,
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
    except Exception:
        return None
    if resp.status_code in (401, 403):
        # Restricted project — we have NO data, not "no AC"
        return {
            "access_denied": True, "title": "", "body": "", "labels": [],
            "acceptance_criteria": "", "has_acceptance_criteria": None,
            "design_doc_links": [],
        }
    if not resp.ok:
        return None
    data = resp.json()
    fields = data.get("fields", {})
    description = fields.get("description", "") or ""
    has_ac, ac_text = _detect_acceptance_criteria(description)
    # A.1: cache raw issue data
    _issue_raw_cache[issue_ref.get("key", key)] = {"key": key, "fields": fields}
    return {
        "title": fields.get("summary", ""),
        "body": description,
        "labels": fields.get("labels", []),
        "acceptance_criteria": ac_text,
        "has_acceptance_criteria": has_ac,
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


def _detect_acceptance_criteria(text: str) -> tuple[bool, str]:
    """Detect acceptance criteria semantically, not just by heading keyword.

    Returns (has_ac, extracted_text). Checks for:
    - Explicit AC headings (Acceptance Criteria, Definition of Done, Requirements)
    - Checkbox lists (- [ ] / - [x])
    - BDD format (Given/When/Then)
    - Numbered requirement lists with testable conditions
    - "must", "should", "shall" requirement language in structured lists
    """
    if not text:
        return False, ""

    # 1. Try explicit section headings (broad set)
    for heading in ("acceptance criteria", "definition of done", "requirements",
                    "expected behavior", "success criteria", "done criteria",
                    "exit criteria", "test criteria", "verification criteria"):
        section = _extract_section(text, heading)
        if section and len(section) > 10:
            return True, section

    # 2. Checkbox lists (- [ ] or - [x] or * [ ])
    checkboxes = re.findall(r'[-*]\s*\[[ xX]\]\s*.+', text)
    if len(checkboxes) >= 2:
        return True, "\n".join(checkboxes[:20])

    # 3. BDD format: Given/When/Then
    bdd = re.findall(
        r'(?:^|\n)\s*(?:Given|When|Then|And|But)\s+.+',
        text, re.IGNORECASE,
    )
    if len(bdd) >= 2:
        return True, "\n".join(bdd[:20])

    # 4. Numbered requirements with modal verbs
    numbered = re.findall(
        r'(?:^|\n)\s*\d+[.)]\s+.+(?:must|should|shall|will|needs? to|has to).+',
        text, re.IGNORECASE,
    )
    if len(numbered) >= 2:
        return True, "\n".join(numbered[:20])

    # 5. Prose behavioral specification — description explicitly states the desired approach/fix.
    # Matches patterns like "Fix — ...", "Revised approach:", "Expected behavior:", "Goal:", etc.
    # Also matches substantial prose (>300 chars) that contains multiple behavioral sentences.
    prose_heading = re.search(
        r'(?:^|\n)\s*(?:Fix\s*[—–-]|Revised approach|Expected behavior|Desired behavior|'
        r'Approach:|Goal:|Objective:|Behavior:|Solution:|The fix:|This PR (?:fixes|adds|changes|wires))',
        text, re.IGNORECASE,
    )
    if prose_heading and len(text.strip()) > 150:
        # Extract the sentence(s) after the prose heading (up to 500 chars)
        start = prose_heading.start()
        return True, text[start:start + 500].strip()

    # 6. Behavioral sentences with clear subject + verb + condition (at least 2)
    behavioral = re.findall(
        r'(?:^|\n)\s*.{10,}(?:will|should|must|needs to|has to|is expected to)'
        r'.{10,}(?:when|if|after|before|until|on|upon).+',
        text, re.IGNORECASE,
    )
    if len(behavioral) >= 2:
        return True, "\n".join(behavioral[:10])

    return False, ""


def _extract_links(text: str) -> list[str]:
    """Extract URLs from text."""
    if not text:
        return []
    return re.findall(r'https?://[^\s<>\)]+', text)


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------

def build_pr_context(prs: list[dict], max_chars: int = 100_000, priority_authors: tuple[str, ...] = ()) -> str:
    """Serialize PR data as structured Markdown for the Claude prompt.

    Per PR: number, title, author, merged date, files changed count,
    linked issue summary, bot comments summary, human comments summary.
    Truncates per-PR to stay within the total character budget. Human review comments are chosen by
    ``select_comments`` (substantive and inline first, one per reviewer before seconds); comments by
    ``priority_authors`` (the ``--reviewers`` list) are always kept, up to 4000 chars each, and extend the
    PR's budget so they are not cut by the final truncation.
    """
    if not prs:
        return "(no PRs available)"

    per_pr_budget = max(max_chars // max(len(prs), 1), 500)
    lines: list[str] = [f"## PRs ({len(prs)} total)\n"]
    total = 0

    for pr in prs:
        section: list[str] = []
        pr_state = pr.get("state", "merged")
        section.append(f"### PR #{pr['number']}: {pr['title']}")
        section.append(f"- **Author**: {pr.get('author', '?')}")
        section.append(f"- **State**: {pr_state}")
        if pr_state == "merged":
            section.append(f"- **Merged**: {pr.get('merged_at', '?')}")
        section.append(f"- **Branch**: {pr.get('branch', '?')}")
        section.append(f"- **Files changed**: {pr.get('files_changed', '?')}")
        additions = pr.get("additions", 0)
        deletions = pr.get("deletions", 0)
        if additions or deletions:
            bucket = pr.get("size_bucket") or size_bucket(additions, deletions)
            section.append(f"- **Lines of code**: +{additions} / -{deletions} (size: {bucket})")
        section.append(f"- **Review decision**: {pr.get('review_decision', 'none')}")
        if pr.get("is_bot_authored"):
            section.append("- **Author type**: AI/bot-authored PR")
        subst = pr.get("substantive_human_comment_count", 0)
        section.append(f"- **Substantive human comments**: {subst}")
        rubber = pr.get("rubber_stamp_approvers", [])
        if rubber:
            section.append(f"- **Rubber-stamp approvers** (approved, 0 substantive comments): {', '.join(rubber)}")

        if pr.get("pr_type"):
            section.append(f"- **PR type**: {pr['pr_type']}")
        if pr.get("category") and pr["category"] != "unknown":
            section.append(f"- **Category**: {pr['category']} (confidence: {pr.get('category_confidence', '?')})")
        if pr.get("blast_radius"):
            section.append(f"- **Blast radius**: {pr['blast_radius']}")
        if pr.get("risk_tier"):
            section.append(f"- **Risk**: score={pr.get('risk_score', 0):.0f}, tier={pr['risk_tier']}")
        if pr.get("complexity") and pr["complexity"] != "low":
            section.append(f"- **Complexity**: {pr['complexity']}")
        if pr.get("is_hotspot"):
            section.append("- **Hotspot**: yes (touches sensitive paths)")

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
                has_ac = details.get("has_acceptance_criteria")  # None = access_denied
                ac = details.get("acceptance_criteria", "")
                if details.get("access_denied"):
                    section.append(issue_line)
                    section.append("  - **has_acceptance_criteria**: unknown (Jira access denied — restricted project)")
                elif has_ac:
                    section.append(issue_line)
                    section.append(f"  - **has_acceptance_criteria**: true")
                    if ac:
                        section.append(f"  - **Acceptance criteria**: {ac[:500]}")
                else:
                    section.append(issue_line)
                    section.append(f"  - **has_acceptance_criteria**: false")
                    section.append("  - **Acceptance criteria**: _(none found)_")
                    issue_body = details.get("body", "")
                    if issue_body:
                        section.append(f"  - **Issue description excerpt**: {issue_body[:600]}")
            else:
                section.append(issue_line)

        # Reviewer summary: separate CI bots, review bots, humans (A.3 + A.4)
        human = pr.get("human_comments", [])
        ci_bot = pr.get("ci_comments", [])
        review_bot = pr.get("review_bot_comments", [])
        # Fallback for PRs fetched before the 3-bucket split
        if not ci_bot and not review_bot:
            bot_all = pr.get("bot_comments", [])
            review_bot = bot_all

        human_authors = sorted({c.get("author", "?") for c in human if c.get("author")})
        review_bot_authors = sorted({c.get("author", "?") for c in review_bot if c.get("author")})
        ci_bot_authors = sorted({c.get("author", "?") for c in ci_bot if c.get("author")})
        reviewer_summary = "none"
        parts = []
        if human_authors:
            parts.append(f"human: {', '.join(human_authors[:5])}")
        if review_bot_authors:
            parts.append(f"review-bots: {', '.join(review_bot_authors[:3])}")
        if ci_bot_authors:
            parts.append(f"ci-bots: {', '.join(ci_bot_authors[:3])}")
        if parts:
            reviewer_summary = " | ".join(parts)
        section.append(f"- **Reviewers**: {reviewer_summary}")
        approvers = pr.get("approvers", [])
        if approvers:
            section.append(f"- **Approved by**: {', '.join(approvers[:5])}")
        elif pr.get("review_decision") == "APPROVED":
            section.append("- **Approved by**: (approval recorded, name unavailable)")

        priority_extra = 0
        if human:
            shown, omitted = select_comments(
                human,
                budget_chars=max(per_pr_budget - _PR_HEADER_RESERVE, _MIN_COMMENT_BUDGET),
                priority_authors=priority_authors,
                is_trivial=lambda b: bool(_RUBBER_STAMP_RE.match(b.strip())),
            )
            section.append(f"- **Human comments** ({len(human)}):")
            prio = {p.lower() for p in priority_authors}
            for c in shown:
                ctype = c.get("type", "comment")
                path = c.get("path", "")
                prefix = f"[{ctype}]" if ctype != "comment" else ""
                file_ref = f" on `{path}`" if path else ""
                is_prio = bool(prio & {(c.get("author") or "").lower(), (c.get("author_alias") or "").lower()})
                if is_prio:
                    priority_extra += len(c.get("body", ""))
                section.append(f"  - @{c.get('author', '?')}{file_ref} {prefix}: {c.get('body', '')}")
            if omitted:
                section.append(f"  - ...and {omitted} more")

        if review_bot:
            section.append(f"- **Code-review bot comments** ({len(review_bot)}):")
            for c in review_bot[:3]:
                body = c.get("body", "")[:300]
                section.append(f"  - @{c.get('author', '?')}: {body}")
            if len(review_bot) > 3:
                section.append(f"  - ...and {len(review_bot) - 3} more")

        if ci_bot:
            section.append(f"- **CI bot comments** ({len(ci_bot)} — build/test status reporters):")
            for c in ci_bot[:2]:
                body = c.get("body", "")[:150]
                section.append(f"  - @{c.get('author', '?')}: {body}")
            if len(ci_bot) > 2:
                section.append(f"  - ...and {len(ci_bot) - 2} more")

        # PR body excerpt
        body = pr.get("body", "")
        if body:
            excerpt = body[:300].replace("\n", " ")
            section.append(f"- **Description**: {excerpt}")

        # A.4: data completeness note
        missing: list[str] = []
        if not pr.get("linked_issue"):
            missing.append("no linked issue")
        if not pr.get("review_decision"):
            missing.append("review_decision unknown")
        if pr.get("files_changed", 0) == 0 and pr.get("additions", 0) == 0:
            missing.append("no file-change data")
        if not pr.get("all_comments"):
            missing.append("no comments")
        if missing:
            section.append(f"- **Data completeness**: missing: {', '.join(missing)}")

        pr_text = "\n".join(section) + "\n"
        pr_limit = per_pr_budget + priority_extra
        if len(pr_text) > pr_limit:
            pr_text = pr_text[:pr_limit] + "\n_(truncated)_\n"

        lines.append(pr_text)
        total += len(pr_text)

    return "\n".join(lines)
