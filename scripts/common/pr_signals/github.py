"""GitHub adapter: review activity for many PRs in a few GraphQL requests.

Returns, per PR number, the tracker-neutral raw record that ``pr_signals.summarize`` turns into metrics. All the
GitHub-specific knowledge (field names, ``__typename`` for bot detection, review threads as tasks) lives here.
"""

from __future__ import annotations

import json
import subprocess

from scripts.common.http_retry import Retryable, call_with_retry

BATCH = 10       # PRs per GraphQL request
TIMEOUT = 90

_ACTOR = "author{login __typename}"
_DECISIONS = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
_FIELDS = {
    "author": _ACTOR,
    "comments": f"comments(first:100){{totalCount nodes{{body {_ACTOR}}}}}",
    "reviews": f"reviews(first:50){{totalCount nodes{{state body {_ACTOR}}}}}",
    "requests": "reviewRequests(first:20){nodes{requestedReviewer{__typename ... on User{login}}}}",
    "threads": f"reviewThreads(first:50){{totalCount nodes{{isResolved comments(first:20){{totalCount nodes{{body {_ACTOR}}}}}}}}}",
    "commits": "commits(first:100){totalCount nodes{commit{parents(first:2){totalCount}}}}",
}


def _wanted(needs: set[str]) -> list[str]:
    """GraphQL fields a set of signal groups needs ("reviewers" also reads comment authors)."""
    fields = ["author"]
    if needs & {"comments", "reviewers"}:
        fields += ["comments", "reviews", "threads"]
    if "reviewers" in needs:
        fields.append("requests")
    if "tasks" in needs and "threads" not in fields:
        fields.append("threads")
    if "commits" in needs:
        fields.append("commits")
    return fields


def build_query(numbers: list[int], needs: set[str]) -> str:
    body = " ".join(f"p{n}: pullRequest(number: {int(n)}) {{ number {' '.join(_FIELDS[f] for f in _wanted(needs))} }}" for n in numbers)
    return f"query($owner:String!,$name:String!){{repository(owner:$owner,name:$name){{ {body} }}}}"


def _run_graphql(query: str, owner: str, repo: str) -> dict:
    try:
        proc = subprocess.run(["gh", "api", "graphql", "-f", f"query={query}", "-F", f"owner={owner}", "-F", f"name={repo}"],
                              capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise Retryable("gh graphql timed out") from e
    except OSError as e:
        raise RuntimeError(f"gh is not available: {type(e).__name__}") from e
    text = (proc.stdout or "") + (proc.stderr or "")
    lowered = text.lower()
    if proc.returncode != 0 and ("rate limit" in lowered or "http 502" in lowered or "http 503" in lowered or "http 504" in lowered):
        raise Retryable("github rate limit or transient error")
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        raise RuntimeError(f"gh api graphql failed (exit {proc.returncode}): {text[:200]}")
    for err in payload.get("errors") or []:
        if err.get("type") == "RATE_LIMITED":
            raise Retryable("github graphql rate limited")
    return payload


def _actor(node: dict | None) -> dict:
    a = (node or {}).get("author") or {}
    return {"name": a.get("login") or "ghost", "kind": a.get("__typename") or ""}


def _cut(node: dict, key: str) -> bool:
    """True when a connection returned fewer nodes than it has (the page size cut it)."""
    return (node.get(key) or {}).get("totalCount", 0) > len((node.get(key) or {}).get("nodes", []))


def _raw(node: dict) -> dict:
    """GraphQL PR node → tracker-neutral raw record."""
    comments, tasks = [], []
    for c in (node.get("comments") or {}).get("nodes", []):
        comments.append({**_actor(c), "body": c.get("body", "")})
    for r in (node.get("reviews") or {}).get("nodes", []):
        if (r.get("body") or "").strip():
            comments.append({**_actor(r), "body": r["body"]})
    for t in (node.get("reviewThreads") or {}).get("nodes", []):
        thread = [{**_actor(c), "body": c.get("body", "")} for c in (t.get("comments") or {}).get("nodes", [])]
        comments.extend(thread)
        if thread:
            tasks.append({**thread[0], "resolved": bool(t.get("isResolved"))})
    review_authors: dict[str, dict] = {}
    for r in (node.get("reviews") or {}).get("nodes", []):
        a = _actor(r)
        if a["name"] == "ghost":
            continue
        entry = review_authors.setdefault(a["name"], {**a, "approved": False})
        if r.get("state") in _DECISIONS:   # a later plain COMMENTED/PENDING review does not withdraw an approval
            entry["approved"] = r["state"] == "APPROVED"
    assigned = []
    for rq in (node.get("reviewRequests") or {}).get("nodes", []):
        who = rq.get("requestedReviewer") or {}
        if who.get("login"):   # team requests are skipped: reading a team's name needs the read:org token scope
            assigned.append({"name": who["login"], "kind": ""})
    commits = (node.get("commits") or {}).get("nodes", [])
    threads = (node.get("reviewThreads") or {}).get("nodes", [])
    threads_cut = _cut(node, "reviewThreads") or any((t.get("comments") or {}).get("totalCount", 0) > len((t.get("comments") or {}).get("nodes", [])) for t in threads)
    truncated = [g for g, hit in (("comments", _cut(node, "comments") or _cut(node, "reviews") or threads_cut), ("tasks", _cut(node, "reviewThreads")), ("commits", _cut(node, "commits"))) if hit]
    return {
        "author": _actor(node)["name"], "comments": comments, "assigned": assigned,
        "review_authors": list(review_authors.values()), "tasks": tasks,
        "commits": sum(1 for c in commits if ((c.get("commit") or {}).get("parents") or {}).get("totalCount", 1) < 2),
        "commits_total": (node.get("commits") or {}).get("totalCount", len(commits)), "truncated": truncated,
    }


def fetch(numbers: list[int], config: dict, needs: set[str]) -> dict[int, dict]:
    """Raw records for ``numbers`` in ``ceil(n / BATCH)`` requests; PRs GitHub does not return are omitted."""
    owner, repo = config.get("GH_ORG", ""), config.get("GH_REPO", "")
    if not (owner and repo):
        raise RuntimeError("GH_ORG and GH_REPO are required to read PR activity")
    out: dict[int, dict] = {}
    for i in range(0, len(numbers), BATCH):
        chunk = numbers[i:i + BATCH]
        try:
            payload = call_with_retry(lambda: _run_graphql(build_query(chunk, needs), owner, repo))
        except (Retryable, RuntimeError) as e:
            out.update({n: {"error": str(e)} for n in chunk})   # explicit, never a silent zero
            continue
        repo_data = ((payload.get("data") or {}).get("repository")) or {}
        errors = "; ".join(e.get("message", "")[:160] for e in payload.get("errors") or [])
        for n in chunk:
            node = repo_data.get(f"p{n}")
            if node:
                out[n] = _raw(node)
            elif errors:
                out[n] = {"error": f"github graphql: {errors}"}   # a rejected query is an error, not "PR not found"
    return out
