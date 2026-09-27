"""Collect open PRs for merge queue analysis — no label required.

Usage:
    python -m scripts.mq.collect_ready                         # single repo (GH_ORG/GH_REPO or BB_WORKSPACE/BB_REPO)
    python -m scripts.mq.collect_ready --repo bhatti/todo-sample  # explicit repo override
    python -m scripts.mq.collect_ready --label ready-to-merge    # optional label filter (GH only)

Works for GitHub and Bitbucket via existing standup fetchers (gather_gh / bb_helpers).
Tracker resolved via DEFAULT_TRACKER or repo URL domain — same logic as all mq scripts.

Reads:  open PR list via gh CLI or Bitbucket REST (no special label needed)
Writes: /workspace/ready_prs.json

Exit codes: 0=done, 1=error
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import click

from scripts.common.config import get_workspace_dir, load_config
from scripts.common.pr_classify import (
    SENSITIVE_PATHS,
    classify_pr_category as _classify_pr_category,
    classify_pr_flags as _classify_pr_flags,
    classify_pr_type as _classify_pr_type,
    enrich_pr_with_metrics as _enrich_pr_with_metrics,
    extract_issue_ref as _extract_issue_ref,
    is_test_file,
)
from scripts.mq._shared import (
    _cfg_for_repo,
    fetch_bb_pr_metadata,
    fetch_open_prs,
    fetch_pr_files,
    repo_slug,
    resolve_tracker,
)


def _fetch_diffstat_for_pr(config: dict, pr_number: str) -> list[dict]:
    """Fetch diffstat for a single PR. Thread-safe wrapper for concurrent use."""
    return fetch_pr_files(config, pr_number)


def _enrich_prs_with_diffstat(prs: list[dict], config: dict) -> None:
    """Enrich each PR's blast_radius, scope, and category in-place using per-PR diffstat.

    Fetches diffstats concurrently (up to DIFFSTAT_WORKERS threads, default 8) to avoid
    serial HTTP bottleneck on large repos. Enrichment logic runs sequentially after fetch.
    Best-effort: failures leave fields at their label/title-derived defaults.
    File paths are the authoritative signal for both category and blast_radius.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from scripts.mq.scope_router import _compute_scope
    import time

    total = len(prs)
    if not total:
        return

    max_workers = int(os.environ.get("DIFFSTAT_WORKERS", "8"))
    max_workers = max(1, min(max_workers, 20))
    print(
        f"[collect_ready] enriching {total} PRs with diffstat "
        f"(concurrent, workers={max_workers})...",
        flush=True,
    )

    t0 = time.monotonic()
    diffstats: dict[int, list[dict]] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_to_pr = {
            pool.submit(_fetch_diffstat_for_pr, config, str(pr["pr_number"])): pr["pr_number"]
            for pr in prs
        }
        done_count = 0
        for future in as_completed(future_to_pr):
            pr_num = future_to_pr[future]
            done_count += 1
            try:
                diffstats[pr_num] = future.result()
            except Exception as exc:
                print(f"[collect_ready] warn: diffstat failed PR#{pr_num}: {exc}", flush=True)
                diffstats[pr_num] = []
            if done_count % 25 == 0:
                print(f"[collect_ready] fetched {done_count}/{total} diffstats...", flush=True)

    elapsed = time.monotonic() - t0
    print(f"[collect_ready] diffstat fetch complete: {total} PRs in {elapsed:.1f}s", flush=True)

    is_bb = resolve_tracker(config) == "bitbucket"
    if is_bb:
        t1 = time.monotonic()
        print(f"[collect_ready] fetching BB build status + metadata (concurrent)...", flush=True)
        metadata: dict[int, dict] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_to_pr = {
                pool.submit(fetch_bb_pr_metadata, config, str(pr["pr_number"])): pr["pr_number"]
                for pr in prs
            }
            for future in as_completed(future_to_pr):
                pr_num = future_to_pr[future]
                try:
                    metadata[pr_num] = future.result()
                except Exception:
                    metadata[pr_num] = {}
        for pr in prs:
            meta = metadata.get(pr["pr_number"], {})
            if meta.get("build_status"):
                pr["ci_status"] = meta["build_status"]
            if meta.get("comment_count") is not None:
                pr["comment_count"] = meta["comment_count"]
            if meta.get("approval_count") is not None:
                pr["approval_count"] = meta["approval_count"]
            if meta.get("reviewer_names"):
                pr["reviewer_names"] = meta["reviewer_names"]
                pr["reviewer_count"] = len(meta["reviewer_names"])
            if meta.get("build_count_total"):
                pr["build_count_total"] = meta["build_count_total"]
                pr["build_count_passed"] = meta.get("build_count_passed", 0)
                pr["build_count_failed"] = meta.get("build_count_failed", 0)
        print(f"[collect_ready] metadata fetch complete in {time.monotonic() - t1:.1f}s", flush=True)

    for pr in prs:
        files = diffstats.get(pr["pr_number"], [])
        if not files:
            continue
        try:
            scope_name, blast_radius, _, _ = _compute_scope(files, {})
            if scope_name and scope_name not in ("unknown", "cross-scope"):
                pr["scope"] = scope_name
            pr["blast_radius"] = blast_radius
            _enrich_pr_with_metrics(pr, files=files)
        except Exception as exc:
            print(f"[collect_ready] warn: enrich failed PR#{pr['pr_number']}: {exc}", flush=True)


def _compute_age_hours(created_at: str) -> float:
    """Compute PR age in hours from ISO 8601 timestamp."""
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created).total_seconds() / 3600
    except (ValueError, AttributeError):
        return 0.0


def _normalize_pr(pr: dict, default_repo: str) -> dict:
    """Normalize a PR dict from gather_gh or bb_helpers to the ready_prs schema.

    gather_gh returns: id (=number), title, author (login str), branch, created,
        age_hours, reviewers, has_approval, approval_count, ci_status, url, labels
    bb_helpers returns: id, title, author (display_name str), branch, created,
        age_hours, reviewers
    Neither returns files/additions/deletions — blast_radius defaults to 'low'.
    scope_router.py does authoritative per-PR file analysis downstream.
    """
    n = pr.get("number")
    pr_number = n if n is not None else pr.get("id", 0)

    # age_hours already computed by both standup fetchers; fall back to timestamp parse
    age_hours = pr.get("age_hours") or _compute_age_hours(
        pr.get("createdAt", "") or pr.get("created", "")
    )

    # author: gather_gh returns login string; bb_helpers returns display_name string
    author = pr.get("author", "")
    if isinstance(author, dict):
        author_login = author.get("login") or author.get("display_name") or "unknown"
    else:
        author_login = str(author) or "unknown"

    # ci_status: gather_gh computes it; bb_helpers doesn't (no BB CI status API used)
    ci_status = pr.get("ci_status", "none")

    # approval: gather_gh has has_approval + approval_count; bb_helpers extracts from participants
    approval_count = pr.get("approval_count", 0)
    has_approval = pr.get("has_approval") or (approval_count > 0)
    reviewer_count = pr.get("reviewer_count") or len(pr.get("reviewers", []))

    # Title-only pass for flags (no files yet); _enrich_prs_with_diffstat refines with file paths
    flags = _classify_pr_flags(pr)
    category, category_confidence = _classify_pr_category(pr)
    if flags["is_test_pr"]:
        category, category_confidence = "test", "title"
    return {
        "pr_number": pr_number,
        "repo": pr.get("repo", default_repo),
        "title": pr.get("title", ""),
        "scope": "unknown",          # _enrich_prs_with_diffstat sets authoritative scope per PR
        "blast_radius": "low",       # _enrich_prs_with_diffstat sets authoritative blast_radius per PR
        "category": category,        # _enrich_prs_with_diffstat upgrades to 'file_path' confidence
        "category_confidence": category_confidence,
        "is_test_pr": flags["is_test_pr"],
        "is_wip_pr": flags["is_wip_pr"],
        "is_docs_pr": flags["is_docs_pr"],
        "pr_type": _classify_pr_type(pr, flags),
        "author": author_login,
        "age_hours": round(float(age_hours), 1),
        "branch": pr.get("headRefName", "") or pr.get("branch", ""),
        "target_branch": pr.get("target_branch", "") or pr.get("baseRefName", ""),
        "ci_status": ci_status,
        "has_approval": bool(has_approval),
        "approval_count": int(approval_count),
        "reviewer_count": int(reviewer_count),
        "url": pr.get("url", ""),
        "labels": pr.get("labels", []),
        "issue_ref": _extract_issue_ref(pr),
        "total_loc": 0,
        "file_count": 0,
        "complexity": "low",
        "is_hotspot": False,
        "risk_score": 0,
        "risk_tier": "low",
        "risk_dimensions": {},
    }


@click.command()
@click.option("--label", default="", help="Optional label filter (empty = all open PRs; GH only)")
@click.option("--repo", default="", help="Repo override: full URL or org/repo slug")
@click.option("--target-branch", "--target", default="", envvar="TARGET_BRANCH",
              help="Only collect PRs targeting this branch (e.g. stage, main, dev). "
                   "Dramatically reduces PR count for large repos.")
def main(label: str, repo: str, target_branch: str) -> None:
    config = load_config(required=[])

    # _cfg_for_repo sets GH_ORG/GH_REPO (or BB equivalents) on a copy; merge it back
    # so repo_slug() and the artifact output reflect the actual repo that was queried.
    if repo:
        config = _cfg_for_repo(config, repo)

    raw_prs = fetch_open_prs(config, label=label, target_branch=target_branch)
    slug = repo_slug(config)
    workspace = get_workspace_dir(config)
    workspace.mkdir(parents=True, exist_ok=True)

    print(f"[collect_ready] repo={slug} target_branch={target_branch!r} label={label!r} prs_found={len(raw_prs)}", flush=True)
    if target_branch and raw_prs:
        from collections import Counter
        branch_dist = Counter(p.get("target_branch", p.get("baseRefName", "?")) for p in raw_prs)
        top3 = branch_dist.most_common(3)
        print(f"[collect_ready] target_branch distribution (top 3): {dict(top3)}", flush=True)

    ready_prs = [_normalize_pr(pr, slug) for pr in raw_prs]

    # Safety filter: enforce target_branch even if upstream fetcher missed some PRs.
    # BB API server-side filtering may silently ignore the q parameter on some endpoints,
    # and pagination can return PRs from other branches on subsequent pages.
    if target_branch:
        before = len(ready_prs)
        ready_prs = [p for p in ready_prs if p.get("target_branch", "") == target_branch]
        if before != len(ready_prs):
            print(
                f"[collect_ready] target_branch safety filter: {before} → {len(ready_prs)} "
                f"(removed {before - len(ready_prs)} PRs not targeting {target_branch!r})",
                flush=True,
            )

    ready_prs.sort(key=lambda p: p["age_hours"], reverse=True)

    # Enrich blast_radius and scope via per-PR diffstat calls (best-effort)
    _enrich_prs_with_diffstat(ready_prs, config)

    result = {
        "pr_count": len(ready_prs),
        "repo": slug,
        "target_branch_filter": target_branch,
        "prs": ready_prs,
    }
    out_path = workspace / "ready_prs.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"[collect_ready] wrote {out_path} ({len(ready_prs)} PRs)", flush=True)
    print(f"::add-task-context TOTAL_PRS::{len(ready_prs)}", flush=True)
    if target_branch:
        print(f"::add-task-context TARGET_BRANCH::{target_branch}", flush=True)


if __name__ == "__main__":
    main()
