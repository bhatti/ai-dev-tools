"""Canonical PR normalization and enrichment.

Single source of truth for PR object normalization across all workflows.
All workflows import from here to guarantee consistent field names and safe defaults.

Public API
----------
compute_pr_age      — days since created_at; 0 if field missing
format_pr_status    — MERGED / OPEN / PENDING / WIP / DECLINED with emoji
normalize_pr        — produce canonical PR dict with all fields guaranteed
enrich_pr_fast      — classification-only enrichment (no extra API calls)
enrich_pr_full      — full enrichment + CI status from API + reviewer details
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import TYPE_CHECKING


# ---------------------------------------------------------------------------
# Age computation
# ---------------------------------------------------------------------------

def compute_pr_age(pr: dict) -> int:
    """Return age in days since created_at; 0 if field is missing or unparseable."""
    created_at = pr.get("created_at") or pr.get("createdAt") or pr.get("created") or ""
    if not created_at:
        return 0
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(created_at[:26], fmt[:len(created_at[:26])])
            dt = dt.replace(tzinfo=timezone.utc)
            now = datetime.now(tz=timezone.utc)
            return max(0, (now - dt).days)
        except (ValueError, TypeError):
            continue
    return 0


# ---------------------------------------------------------------------------
# Status formatting
# ---------------------------------------------------------------------------

def format_pr_status(pr: dict) -> str:
    """Return a short status string with emoji prefix.

    Priority: merged → declined → wip → pending (no substantive review) → open
    """
    if pr.get("merged_at") or pr.get("mergedAt") or pr.get("state", "").lower() == "merged":
        return "🟣 MERGED"
    state = (pr.get("state") or "").lower()
    if state in ("declined", "closed") and not pr.get("merged_at"):
        return "🔴 DECLINED"
    if pr.get("is_wip_pr"):
        return "🔵 WIP"
    if not pr.get("has_substantive_review", False):
        return "🟡 PENDING"
    return "🟢 OPEN"


# ---------------------------------------------------------------------------
# CI status normalization
# ---------------------------------------------------------------------------

_BB_CI_MAP = {
    "successful": "pass", "success": "pass",
    "failed": "fail", "failure": "fail",
    "inprogress": "pending", "pending": "pending",
    "none": "unknown",
}
_GH_CI_MAP = {
    "success": "pass",
    "failure": "fail", "error": "fail",
    "pending": "pending",
}


def _normalize_ci_status(pr: dict) -> str:
    """Derive ci_status from build_status (BB) or check_state / checkRunState (GH)."""
    # Already computed by a previous call
    if pr.get("ci_status") and pr["ci_status"] != "unknown":
        return pr["ci_status"]
    # Bitbucket raw build_status
    raw = (pr.get("build_status") or "").lower()
    if raw:
        return _BB_CI_MAP.get(raw, "unknown")
    # GitHub check_state / checkRunState / statusCheckRollup
    raw = (pr.get("check_state") or pr.get("checkRunState") or "").lower()
    if raw:
        return _GH_CI_MAP.get(raw, "unknown")
    return "unknown"


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_KNOWN_SIZE_BUCKETS = ("xs", "s", "m", "l", "xl")


def _size_bucket(loc: int) -> str:
    if loc < 50:
        return "xs"
    if loc < 200:
        return "s"
    if loc < 500:
        return "m"
    if loc < 1000:
        return "l"
    return "xl"


def normalize_pr(raw_pr: dict, tracker: str = "github") -> dict:
    """Produce a canonical PR dict with all fields guaranteed.

    Starts from raw_pr and fills any missing fields with safe defaults.
    Does NOT call enrichment (no classification side-effects) — call
    enrich_pr_fast() or enrich_pr_full() for classification fields.

    Fields guaranteed after normalization:
        number, title, author, state, merged_at, created_at, updated_at, url, branch,
        files_changed, additions, deletions, size_bucket,
        review_decision, approvers, reviewers, reviewer_count,
        ci_status, build_status, is_bot_authored,
        ci_comments, review_bot_comments, human_comments,
        substantive_human_comment_count, rubber_stamp_approvers, has_substantive_review,
        linked_issue, has_acceptance_criteria,
        category, category_confidence, blast_radius, risk_score, risk_tier, complexity,
        is_hotspot, pr_type, age_days, status_label
    """
    pr = dict(raw_pr)  # shallow copy — don't mutate caller's dict

    # --- Core fields ---
    pr.setdefault("number", pr.get("id") or pr.get("pr_number") or "")
    pr.setdefault("title", "")
    pr.setdefault("author", "")
    pr.setdefault("state", "open")
    pr.setdefault("merged_at", None)
    pr.setdefault("created_at", pr.get("createdAt") or pr.get("created") or "")
    pr.setdefault("updated_at", pr.get("updatedAt") or pr.get("updated") or "")
    pr.setdefault("url", "")
    pr.setdefault("branch", pr.get("headRefName") or pr.get("source_branch") or pr.get("head_ref") or "")
    pr.setdefault("body", pr.get("description") or "")

    # --- Size fields ---
    additions = int(pr.get("additions", 0) or 0)
    deletions = int(pr.get("deletions", 0) or 0)
    pr.setdefault("additions", additions)
    pr.setdefault("deletions", deletions)
    total_loc = additions + deletions
    pr.setdefault("files_changed", pr.get("changedFiles") or pr.get("file_count") or 0)
    if pr.get("size_bucket") not in _KNOWN_SIZE_BUCKETS:
        pr["size_bucket"] = _size_bucket(total_loc)

    # --- Review fields ---
    pr.setdefault("review_decision", pr.get("reviewDecision") or "")
    pr.setdefault("approvers", pr.get("approvers") or [])
    reviewers_raw = pr.get("reviewers") or pr.get("reviewer_names") or []
    if not isinstance(reviewers_raw, list):
        reviewers_raw = []
    pr["reviewers"] = reviewers_raw
    pr["reviewer_count"] = len(reviewers_raw)

    # --- CI status ---
    pr.setdefault("build_status", "")
    pr["ci_status"] = _normalize_ci_status(pr)

    # --- Bot / comment fields ---
    pr.setdefault("is_bot_authored", False)
    pr.setdefault("ci_comments", pr.get("ci_comments") or [])
    pr.setdefault("review_bot_comments", pr.get("review_bot_comments") or [])
    pr.setdefault("human_comments", pr.get("human_comments") or [])
    pr.setdefault("substantive_human_comment_count", 0)
    pr.setdefault("rubber_stamp_approvers", [])
    pr.setdefault("has_substantive_review", False)

    # --- Issue / AC fields ---
    pr.setdefault("linked_issue", None)
    pr.setdefault("has_acceptance_criteria", None)

    # --- Classification fields (safe defaults; set by enrich_pr_fast/full) ---
    pr.setdefault("category", "unknown")
    pr.setdefault("category_confidence", "unknown")
    pr.setdefault("blast_radius", "low")
    pr.setdefault("risk_score", 0)
    pr.setdefault("risk_tier", "low")
    pr.setdefault("complexity", "low")
    pr.setdefault("is_hotspot", False)
    pr.setdefault("pr_type", "unknown")

    # --- Derived fields ---
    pr["age_days"] = compute_pr_age(pr)
    pr["status_label"] = format_pr_status(pr)

    return pr


# ---------------------------------------------------------------------------
# Enrichment helpers
# ---------------------------------------------------------------------------

def enrich_pr_fast(pr: dict, files: list | None = None) -> dict:
    """Lightweight enrichment — runs classification from local data only.

    No extra API calls. Sets category, blast_radius, risk_score, complexity,
    is_hotspot, pr_type, age_days, status_label, reviewer_count.

    Returns the same dict (in-place + returned for chaining).
    """
    from scripts.common.pr_classify import enrich_pr_with_metrics

    # Ensure safe defaults before enrichment
    pr.setdefault("age_days", compute_pr_age(pr))
    pr.setdefault("ci_status", _normalize_ci_status(pr))
    reviewers = pr.get("reviewers") or pr.get("reviewer_names") or []
    pr["reviewer_count"] = len(reviewers) if isinstance(reviewers, list) else 0

    enrich_pr_with_metrics(pr, files)  # sets category, blast_radius, risk_score, etc.

    pr["age_days"] = compute_pr_age(pr)   # recompute in case created_at was set
    pr["status_label"] = format_pr_status(pr)
    return pr


def enrich_pr_full(pr: dict, files: list | None = None, config: dict | None = None) -> dict:
    """Full enrichment — classification + CI status from API (BB only for now).

    Calls enrich_pr_fast() then optionally fetches CI/reviewer details from
    the Bitbucket API when tracker == 'bitbucket' and credentials are present.

    Returns the same dict (in-place + returned for chaining).
    """
    enrich_pr_fast(pr, files)

    cfg = config or {}
    tracker = (cfg.get("DEFAULT_TRACKER") or "github").lower()
    if tracker in ("bitbucket", "jira", "jira/bitbucket"):
        pr_number = str(pr.get("number") or pr.get("id") or pr.get("pr_number") or "")
        if pr_number:
            try:
                from scripts.mq._shared import fetch_bb_pr_metadata
                meta = fetch_bb_pr_metadata(cfg, pr_number)
                if meta:
                    if "build_status" in meta:
                        pr["build_status"] = meta["build_status"]
                        pr["ci_status"] = _BB_CI_MAP.get(meta["build_status"].lower(), "unknown")
                    if "reviewer_names" in meta:
                        pr["reviewers"] = meta["reviewer_names"]
                        pr["reviewer_count"] = len(meta["reviewer_names"])
                    if "approval_count" in meta:
                        pr["approval_count"] = meta["approval_count"]
                    if "comment_count" in meta:
                        pr["comment_count"] = meta["comment_count"]
                    if "build_count_total" in meta:
                        pr["build_count_total"] = meta["build_count_total"]
                        pr["build_count_passed"] = meta.get("build_count_passed", 0)
                        pr["build_count_failed"] = meta.get("build_count_failed", 0)
            except Exception as exc:
                print(f"[pr-metadata] warn: full enrichment API call failed PR#{pr_number}: {exc}", flush=True)

    pr["status_label"] = format_pr_status(pr)
    return pr
