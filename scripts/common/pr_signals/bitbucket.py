"""Bitbucket Cloud adapter: review activity per PR with partial responses and conditional calls.

Per PR: one small PR call (counts, reviewers, participants), then ``/comments`` (only when
``comment_count`` is non-zero), ``/tasks`` and ``/commits``, each only for the groups requested. Returns the same tracker-neutral
raw record as the GitHub adapter.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import requests

from scripts.common.bitbucket_api import api_base, auth
from scripts.common.config import worker_count
from scripts.common.pr_signals.retry import Retryable, call_with_retry

TIMEOUT = 30
MAX_PAGES = 10          # 10 x 100 items per list; beyond that the group is reported as truncated
PAGELEN = 100

def _user_fields(prefix: str) -> str:
    """Partial-response fields for a user object; every field needs the full path (``a.b,c`` would mean top-level ``c``)."""
    return f"{prefix}.display_name,{prefix}.account_type"


_PR_FIELDS = (f"{_user_fields('author')},comment_count,task_count,{_user_fields('reviewers')},"
              f"participants.role,participants.approved,{_user_fields('participants.user')}")


def _get(url: str, config: dict, params: dict | None = None) -> dict | None:
    """GET JSON; None for 404; :class:`Retryable` for 429/5xx; RuntimeError for other failures."""
    def once() -> dict | None:
        try:
            resp = requests.get(url, auth=auth(config), params=params, timeout=TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as e:
            raise Retryable(type(e).__name__) from e
        except requests.RequestException as e:
            raise RuntimeError(f"bitbucket request failed: {type(e).__name__}") from e
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After", "")
            raise Retryable(f"bitbucket HTTP {resp.status_code}", float(retry_after) if retry_after.replace(".", "", 1).isdigit() else None)
        if resp.status_code == 404:
            return None
        if not resp.ok:
            raise RuntimeError(f"bitbucket HTTP {resp.status_code} for {url}")
        try:
            return resp.json()
        except ValueError as e:
            raise RuntimeError("bitbucket returned a non-JSON response") from e
    return call_with_retry(once)


def _pages(url: str, config: dict, fields: str) -> tuple[list[dict], bool]:
    """All ``values`` of a paginated list (up to MAX_PAGES) and whether more were left."""
    items: list[dict] = []
    params: dict | None = {"pagelen": PAGELEN, "fields": f"next,{fields}"}
    nxt: str | None = url
    for _ in range(MAX_PAGES):
        data = _get(nxt, config, params)
        if data is None:   # a list that exists on every PR must not 404: that is an error, not an empty list
            raise RuntimeError(f"bitbucket HTTP 404 for {nxt}")
        items.extend(data.get("values", []))
        nxt, params = data.get("next"), None   # `next` is an absolute URL that already carries the query
        if not nxt:
            return items, False
    return items, True


def _user(u: dict | None) -> dict:
    u = u or {}
    return {"name": u.get("display_name") or "unknown", "kind": u.get("account_type") or ""}


def _fetch_one_raw(number: int, config: dict, needs: set[str]) -> dict | None:
    ws, repo = config.get("BITBUCKET_WORKSPACE", ""), config.get("BITBUCKET_REPO", "")
    base = f"{api_base()}/repositories/{ws}/{repo}/pullrequests/{number}"
    pr = _get(base, config, {"fields": _PR_FIELDS})
    if pr is None:
        return None
    wants_comments = bool(needs & {"comments", "reviewers"})
    comments: list[dict] = []
    truncated: list[str] = []
    if wants_comments and pr.get("comment_count", 1):
        values, more = _pages(f"{base}/comments", config, f"values.deleted,values.content.raw,{_user_fields('values.user')}")
        comments = [{**_user(c.get("user")), "body": (c.get("content") or {}).get("raw", "")} for c in values if not c.get("deleted")]
        if more:
            truncated.append("comments")
    tasks: list[dict] = []
    if "tasks" in needs:   # task_count is not trusted to include resolved tasks, so it never gates the call
        values, more = _pages(f"{base}/tasks", config, f"values.state,values.content.raw,{_user_fields('values.creator')}")
        tasks = [{**_user(t.get("creator")), "body": (t.get("content") or {}).get("raw", ""),
                  "resolved": (t.get("state") or "").upper() == "RESOLVED"} for t in values]
        if more:
            truncated.append("tasks")
    commits = 0
    if "commits" in needs:
        values, more = _pages(f"{base}/commits", config, "values.hash,values.parents.hash")
        commits = sum(1 for c in values if len(c.get("parents") or []) < 2)
        if more:
            truncated.append("commits")
    participants = pr.get("participants") or []
    return {
        "author": _user(pr.get("author"))["name"], "comments": comments,
        "assigned": [_user(u) for u in pr.get("reviewers") or []],
        "review_authors": [{**_user(p.get("user")), "approved": bool(p.get("approved"))}
                           for p in participants if p.get("role") == "REVIEWER" or p.get("approved")],
        "tasks": tasks, "commits": commits, "commits_total": commits, "truncated": truncated,
    }


def _fetch_one(number: int, config: dict, needs: set[str]) -> dict | None:
    """One PR's raw record; a failure becomes an explicit ``error`` record, never a silent zero."""
    try:
        return _fetch_one_raw(number, config, needs)
    except Exception as e:  # noqa: BLE001 - one PR's failure (HTTP, bad JSON, missing credentials) must not sink the batch
        return {"error": f"{type(e).__name__}: {e}"}


def fetch(numbers: list[int], config: dict, needs: set[str]) -> dict[int, dict]:
    """Raw records for ``numbers``, fetched concurrently (``DIFFSTAT_WORKERS`` workers, default 8, 1-20)."""
    with ThreadPoolExecutor(max_workers=worker_count()) as pool:
        results = list(pool.map(lambda n: _fetch_one(n, config, needs), numbers))
    return {n: r for n, r in zip(numbers, results) if r is not None}
