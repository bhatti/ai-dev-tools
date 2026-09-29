"""Tests for scripts/common/pr_metadata.py — canonical PR normalization and enrichment.

Covers normalize_pr(), compute_pr_age(), format_pr_status(), enrich_pr_fast().
Does NOT test enrich_pr_full() Bitbucket API calls (requires live credentials).
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from scripts.common.pr_metadata import (
    compute_pr_age,
    enrich_pr_fast,
    format_pr_status,
    normalize_pr,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _gh_raw(**kw) -> dict:
    """Minimal GitHub raw PR dict."""
    pr = {
        "number": 42,
        "title": "feat: add payment gateway",
        "body": "Implements Stripe",
        "headRefName": "feature/payments",
        "state": "open",
        "merged_at": None,
        "createdAt": "2026-09-01T10:00:00Z",
        "additions": 200,
        "deletions": 30,
        "changedFiles": 8,
        "url": "https://github.com/acme/app/pull/42",
        "labels": [{"name": "feature"}],
        "reviewers": ["alice", "bob"],
        "approvers": [],
        "has_substantive_review": False,
    }
    pr.update(kw)
    return pr


def _bb_raw(**kw) -> dict:
    """Minimal Bitbucket raw PR dict."""
    pr = {
        "id": 99,
        "title": "fix: null pointer in payments",
        "description": "Fixes NPE on checkout",
        "source_branch": "fix/payments-npe",
        "state": "OPEN",
        "merged_at": None,
        "created_at": "2026-09-15T08:30:00Z",
        "additions": 50,
        "deletions": 10,
        "files_changed": 3,
        "url": "https://bitbucket.org/acme/app/pull-requests/99",
        "labels": [],
        "reviewers": ["carol"],
        "build_status": "SUCCESSFUL",
        "has_substantive_review": True,
    }
    pr.update(kw)
    return pr


# ---------------------------------------------------------------------------
# compute_pr_age
# ---------------------------------------------------------------------------

class TestComputePrAge:
    def test_returns_zero_when_no_created_at(self):
        assert compute_pr_age({}) == 0

    def test_computes_age_from_created_at(self):
        created = (datetime.now(tz=timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pr = {"created_at": created}
        age = compute_pr_age(pr)
        assert 4 <= age <= 6, f"Expected ~5 days, got {age}"

    def test_handles_createdAt_camel_case(self):
        created = (datetime.now(tz=timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pr = {"createdAt": created}
        age = compute_pr_age(pr)
        assert 2 <= age <= 4

    def test_handles_fractional_seconds(self):
        created = (datetime.now(tz=timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        pr = {"created_at": created}
        age = compute_pr_age(pr)
        assert 9 <= age <= 11

    def test_non_negative(self):
        future = (datetime.now(tz=timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert compute_pr_age({"created_at": future}) == 0

    def test_bb_microseconds_with_timezone_offset(self):
        # Bitbucket returns "2026-09-15T08:30:00.000000+00:00" — strptime[:26] fails; fromisoformat handles it
        created = (datetime.now(tz=timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.000000+00:00")
        age = compute_pr_age({"created_at": created})
        assert 6 <= age <= 8, f"Expected ~7 days for BB microsecond format, got {age}"

    def test_invalid_date_returns_zero(self):
        assert compute_pr_age({"created_at": "not-a-date"}) == 0


# ---------------------------------------------------------------------------
# format_pr_status (also tested in test_pr_classify.py — redundant but safe)
# ---------------------------------------------------------------------------

class TestFormatPrStatus:
    def test_merged(self):
        pr = {"merged_at": "2026-01-01T00:00:00Z", "state": "open"}
        assert format_pr_status(pr) == "🟣 MERGED"

    def test_declined(self):
        assert format_pr_status({"state": "declined"}) == "🔴 DECLINED"

    def test_closed_no_merge(self):
        assert format_pr_status({"state": "closed", "merged_at": None}) == "🔴 DECLINED"

    def test_wip(self):
        pr = {"state": "open", "is_wip_pr": True}
        assert format_pr_status(pr) == "🔵 WIP"

    def test_pending(self):
        pr = {"state": "open", "has_substantive_review": False}
        assert format_pr_status(pr) == "🟡 PENDING"

    def test_open(self):
        pr = {"state": "open", "has_substantive_review": True}
        assert format_pr_status(pr) == "🟢 OPEN"


# ---------------------------------------------------------------------------
# normalize_pr — field guarantee
# ---------------------------------------------------------------------------

class TestNormalizePr:
    def test_gh_pr_all_fields_present(self):
        pr = normalize_pr(_gh_raw())
        required = [
            "number", "title", "author", "state", "merged_at", "created_at",
            "updated_at", "url", "branch", "files_changed", "additions", "deletions",
            "size_bucket", "review_decision", "approvers", "reviewers", "reviewer_count",
            "ci_status", "build_status", "is_bot_authored",
            "ci_comments", "review_bot_comments", "human_comments",
            "substantive_human_comment_count", "rubber_stamp_approvers", "has_substantive_review",
            "linked_issue", "has_acceptance_criteria",
            "category", "category_confidence", "blast_radius", "risk_score",
            "risk_tier", "complexity", "is_hotspot", "pr_type",
            "age_days", "status_label",
        ]
        for field in required:
            assert field in pr, f"Missing guaranteed field: {field}"

    def test_bb_pr_all_fields_present(self):
        pr = normalize_pr(_bb_raw(), tracker="bitbucket")
        assert "number" in pr
        assert "ci_status" in pr
        assert "age_days" in pr
        assert "status_label" in pr

    def test_does_not_mutate_original(self):
        raw = _gh_raw()
        original_keys = set(raw.keys())
        normalize_pr(raw)
        assert set(raw.keys()) == original_keys

    def test_reviewer_count_from_list(self):
        pr = normalize_pr(_gh_raw(reviewers=["alice", "bob", "carol"]))
        assert pr["reviewer_count"] == 3

    def test_reviewer_count_empty(self):
        pr = normalize_pr(_gh_raw(reviewers=[]))
        assert pr["reviewer_count"] == 0

    def test_bb_build_status_successful_to_ci_pass(self):
        pr = normalize_pr(_bb_raw(build_status="SUCCESSFUL"))
        assert pr["ci_status"] == "pass"

    def test_bb_build_status_failed_to_ci_fail(self):
        pr = normalize_pr(_bb_raw(build_status="FAILED"))
        assert pr["ci_status"] == "fail"

    def test_bb_build_status_inprogress_to_ci_pending(self):
        pr = normalize_pr(_bb_raw(build_status="INPROGRESS"))
        assert pr["ci_status"] == "pending"

    def test_bb_build_status_error_to_ci_fail(self):
        pr = normalize_pr(_bb_raw(build_status="ERROR"))
        assert pr["ci_status"] == "fail"

    def test_bb_build_status_stopped_to_ci_unknown(self):
        pr = normalize_pr(_bb_raw(build_status="STOPPED"))
        assert pr["ci_status"] == "unknown"

    def test_bb_build_status_none_to_ci_unknown(self):
        pr = normalize_pr(_bb_raw(build_status="NONE"))
        assert pr["ci_status"] == "unknown"

    def test_gh_check_state_success(self):
        pr = normalize_pr(_gh_raw(check_state="SUCCESS"))
        assert pr["ci_status"] == "pass"

    def test_gh_check_state_failure(self):
        pr = normalize_pr(_gh_raw(check_state="FAILURE"))
        assert pr["ci_status"] == "fail"

    def test_ci_unknown_when_no_info(self):
        pr = normalize_pr(_gh_raw())  # no build_status or check_state
        assert pr["ci_status"] == "unknown"

    def test_size_bucket_xs(self):
        pr = normalize_pr(_gh_raw(additions=20, deletions=10))
        assert pr["size_bucket"] == "xs"

    def test_size_bucket_xl(self):
        pr = normalize_pr(_gh_raw(additions=900, deletions=200))
        assert pr["size_bucket"] == "xl"

    def test_branch_from_headRefName(self):
        pr = normalize_pr(_gh_raw(headRefName="feature/foo"))
        assert pr["branch"] == "feature/foo"

    def test_branch_from_source_branch(self):
        pr = normalize_pr(_bb_raw())
        assert pr["branch"] == "fix/payments-npe"

    def test_status_label_computed(self):
        pr = normalize_pr(_bb_raw(state="OPEN", merged_at=None))
        assert pr["status_label"] in ("🟡 PENDING", "🟢 OPEN", "🔵 WIP")

    def test_age_days_computed(self):
        created = (datetime.now(tz=timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pr = normalize_pr(_gh_raw(createdAt=created))
        assert 6 <= pr["age_days"] <= 8

    def test_safe_defaults_for_empty_dict(self):
        pr = normalize_pr({})
        assert pr["number"] == ""
        assert pr["title"] == ""
        assert pr["state"] == "open"
        assert pr["ci_status"] == "unknown"
        assert pr["reviewer_count"] == 0
        assert pr["age_days"] == 0


# ---------------------------------------------------------------------------
# enrich_pr_fast — classification without API calls
# ---------------------------------------------------------------------------

class TestEnrichPrFast:
    def test_sets_classification_fields(self):
        pr = {"title": "fix: auth token validation", "blast_radius": "low",
              "state": "open", "merged_at": None, "additions": 100, "deletions": 20}
        files = [{"path": "src/auth/token.py", "additions": 100, "deletions": 20}]
        result = enrich_pr_fast(pr, files)
        assert result is pr  # in-place
        assert result.get("category") is not None
        assert result.get("risk_tier") in ("low", "medium", "high")
        assert result.get("pr_type") is not None
        assert result.get("age_days") is not None
        assert result.get("status_label") is not None

    def test_no_api_calls_needed(self):
        pr = {"title": "chore: update deps", "state": "open", "merged_at": None}
        enrich_pr_fast(pr, files=None)
        assert pr.get("pr_type") == "chore"

    def test_sets_reviewer_count(self):
        pr = {"title": "feat: new page", "state": "open", "merged_at": None,
              "reviewers": ["x", "y", "z"]}
        enrich_pr_fast(pr, files=None)
        assert pr["reviewer_count"] == 3

    def test_missing_ci_defaults_to_unknown(self):
        pr = {"title": "docs: update readme", "state": "open", "merged_at": None}
        enrich_pr_fast(pr, files=None)
        assert pr.get("ci_status") == "unknown"

    def test_wip_pr_status(self):
        pr = {"title": "[WIP] refactor payments", "state": "open", "merged_at": None}
        enrich_pr_fast(pr, files=None)
        assert pr["status_label"] == "🔵 WIP"
