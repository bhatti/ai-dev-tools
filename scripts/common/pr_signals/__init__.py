"""PR review signals: who commented, who reviewed, how many tasks, how many commits, how many major issues.

``collect_signals`` is the only entry point. Tracker differences stay in the ``github`` / ``bitbucket`` adapters,
which return one tracker-neutral raw record per PR; :func:`summarize` is the single place that turns raw records
into the metrics every report shows, so GitHub and Bitbucket PRs are always measured the same way.

Only the requested groups are fetched (``needs``): ``comments``, ``reviewers``, ``tasks``, ``commits``. Results are
cached per run in ``$WORKSPACE_DIR/pr_signals.json`` so a pipeline never fetches the same PR twice.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from scripts.common.actors import is_bot
from scripts.common.pr_signals import bitbucket, github
from scripts.common.severity import comment_severity

GROUPS = ("comments", "reviewers", "tasks", "commits")
CACHE_NAME = "pr_signals.json"


def enabled() -> bool:
    """False when ``PR_SIGNALS=off`` (opt-out for very large repos)."""
    return os.environ.get("PR_SIGNALS", "on").lower() not in ("off", "0", "false")


def tracker_name(default_tracker: str) -> str:
    """``"github"`` or ``"bitbucket"`` for a DEFAULT_TRACKER value (``jira`` and ``jira/bitbucket`` are Bitbucket PRs)."""
    return "bitbucket" if (default_tracker or "").lower().strip() in ("jira", "jira/bitbucket", "bitbucket") else "github"


def _cache_path(workspace: Path | None = None) -> Path:
    return (Path(workspace) if workspace else Path(os.environ.get("WORKSPACE_DIR", "."))) / CACHE_NAME


def _repo_key(tracker: str, config: dict) -> str:
    return f"{config.get('BITBUCKET_WORKSPACE', '')}/{config.get('BITBUCKET_REPO', '')}" if tracker == "bitbucket" else f"{config.get('GH_ORG', '')}/{config.get('GH_REPO', '')}"


def _read_cache_file(workspace: Path | None) -> dict:
    try:
        data = json.loads(_cache_path(workspace).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("prs"), dict) else {}


def _load_cache(tracker: str, repo: str, workspace: Path | None = None) -> dict:
    data = _read_cache_file(workspace)
    return data["prs"] if data.get("tracker") == tracker and data.get("repo") == repo else {}


def _save_cache(tracker: str, repo: str, prs: dict, workspace: Path | None = None) -> None:
    path = _cache_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tracker": tracker, "repo": repo, "prs": prs}, indent=2), encoding="utf-8")
    except OSError:
        pass  # the cache is an optimisation


def read_cached(number: int | str, workspace: Path | None = None) -> dict | None:
    """The cached record for one PR from ``<workspace>/pr_signals.json`` (written by an earlier pipeline step)."""
    record = _read_cache_file(workspace).get("prs", {}).get(str(number))
    return record if isinstance(record, dict) else None


def _reviewers(raw: dict) -> list[dict]:
    """Union of assigned reviewers, review authors and commenters (bots included), minus the PR author."""
    author = (raw.get("author") or "").lower()
    merged: dict[str, dict] = {}

    def add(name: str, kind: str, source: str, approved: bool = False) -> None:
        key = name.lower()
        if not name or key == author or key == "ghost":
            return
        entry = merged.setdefault(key, {"name": name, "is_bot": is_bot(name, "", kind), "approved": False, "sources": []})
        entry["is_bot"] = entry["is_bot"] or is_bot(name, "", kind)
        entry["approved"] = entry["approved"] or approved
        if source not in entry["sources"]:
            entry["sources"].append(source)

    for a in raw.get("assigned", []):
        add(a["name"], a.get("kind", ""), "assigned")
    for r in raw.get("review_authors", []):
        add(r["name"], r.get("kind", ""), "review", r.get("approved", False))
    for c in raw.get("comments", []):
        add(c["name"], c.get("kind", ""), "comment")
    return sorted(merged.values(), key=lambda e: e["name"].lower())


def summarize(raw: dict, needs: set[str]) -> dict:
    """Tracker-neutral raw record → the metrics record (only the groups in ``needs`` are filled)."""
    if raw.get("error"):
        return {"error": raw["error"], "groups": []}
    rec: dict = {"groups": sorted(needs), "truncated": raw.get("truncated", []), "error": ""}
    comments = raw.get("comments", [])
    if "comments" in needs:
        human = bot = major_human = major_bot = 0
        for c in comments:
            bot_comment = is_bot(c["name"], c.get("body", ""), c.get("kind", ""))
            major = bool(comment_severity(c.get("body", "")))
            bot += bot_comment
            human += not bot_comment
            major_bot += bot_comment and major
            major_human += (not bot_comment) and major
        rec.update(comments_human=human, comments_bot=bot, major_comments_human=major_human, major_comments_bot=major_bot)
    if "reviewers" in needs:
        reviewers = _reviewers(raw)
        rec.update(reviewers=reviewers, reviewers_human=sum(not r["is_bot"] for r in reviewers),
                   reviewers_bot=sum(r["is_bot"] for r in reviewers),
                   approvers=[r["name"] for r in reviewers if r["approved"]])
    if "tasks" in needs:
        tasks = raw.get("tasks", [])
        bot_tasks = [t for t in tasks if is_bot(t["name"], t.get("body", ""), t.get("kind", ""))]
        rec.update(tasks_human=len(tasks) - len(bot_tasks), tasks_bot=len(bot_tasks),
                   tasks_unresolved=sum(not t.get("resolved") for t in tasks))
    if "commits" in needs:
        rec["commits"] = raw.get("commits", 0)
    return rec


def from_classified_comments(pr: dict) -> dict:
    """The ``comments`` and ``reviewers`` groups from a PR dict that already carries classified comments.

    PR audit fetches every comment body anyway (for its own analysis), so these two groups cost no extra calls there.
    Expects ``human_comments`` / ``bot_comments`` (``pr_fetcher.classify_comments``), ``reviewers`` and ``approvers``.
    """
    raw = {
        "author": pr.get("author", ""),
        "comments": [{"name": c.get("author", ""), "body": c.get("body", ""), "kind": c.get("author_kind", "")} for c in pr.get("human_comments", []) + pr.get("bot_comments", [])],
        "assigned": [{"name": n, "kind": ""} for n in pr.get("reviewers", []) or []],
        "review_authors": [{"name": n, "kind": "", "approved": True} for n in pr.get("approvers", []) or []],
    }
    return summarize(raw, {"comments", "reviewers"})


def pr_number(pr: dict) -> int | None:
    """The PR number of a PR dict (``number``, ``id`` or ``pr_number``), or None."""
    n = pr.get("number", pr.get("id", pr.get("pr_number")))
    try:
        return int(n)
    except (TypeError, ValueError):
        return None


def collect_signals(prs: list[dict], tracker: str, config: dict, needs: tuple[str, ...] | set[str] = GROUPS,
                    workspace: Path | None = None) -> dict[int, dict]:
    """Metrics records keyed by PR number for ``prs`` (dicts with ``number`` or ``id``).

    ``tracker`` is ``"github"`` or ``"bitbucket"``; ``workspace`` (default ``$WORKSPACE_DIR``) holds the per-run cache. A PR whose fetch failed has ``error`` set and no metrics;
    reports show it as unavailable instead of zero. ``PR_SIGNALS=off`` disables fetching (reports then show ``—``).
    """
    if not enabled():
        return {}
    wanted = set(needs) & set(GROUPS)
    repo = _repo_key(tracker, config)
    cache = _load_cache(tracker, repo, workspace)
    numbers = [n for n in (pr_number(p) for p in prs) if n is not None]
    missing = [n for n in numbers if not (str(n) in cache and wanted <= set(cache[str(n)].get("groups", [])) and not cache[str(n)].get("error"))]
    if missing:
        raw = (bitbucket if tracker == "bitbucket" else github).fetch(missing, config, wanted)
        for n in missing:
            record = summarize(raw[n], wanted) if n in raw else {"error": "not found", "groups": []}
            old = cache.get(str(n), {})
            if record.get("error") and old.get("groups") and not old.get("error"):
                continue   # keep the good record we already have; this fetch failed
            if not record.get("error") and old.get("groups"):   # keep earlier groups when a later call asked for more
                record = {**old, **record, "groups": sorted(set(old["groups"]) | set(record["groups"]))}
            cache[str(n)] = record
        _save_cache(tracker, repo, cache, workspace)
    return {n: cache[str(n)] for n in numbers if str(n) in cache}
