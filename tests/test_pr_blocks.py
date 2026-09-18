"""Tests for slack_client.build_pr_blocks — grouping and CI status display."""
from __future__ import annotations

import pytest

from scripts.standup.slack_client import build_pr_blocks


def _pr(**kwargs) -> dict:
    base = {
        "id": "1",
        "title": "Fix bug",
        "author": "alice",
        "url": "https://github.com/org/repo/pull/1",
        "jira_url": "",
        "age_days": 2.0,
        "jira_key": "",
        "jira_summary": "",
        "jira_status": "",
        "priority": "",
        "labels": [],
        "reviewers": [],
        "approved_by": [],
        "changes_requested_by": [],
        "approval_count": 0,
        "ci_status": "none",
    }
    base.update(kwargs)
    return base


def _groups(pr_data: dict) -> list[str]:
    """Return the group header texts from build_pr_blocks output (skips title + divider)."""
    blocks = build_pr_blocks("Test", pr_data)
    headers = [b for b in blocks if b["type"] == "header"]
    # First header is the title; group headers start at index 1
    return [h["text"]["text"] for h in headers[1:]]


def _lines(pr_data: dict) -> list[str]:
    """Return the mrkdwn text lines for each PR section block."""
    blocks = build_pr_blocks("Test", pr_data)
    return [
        b["text"]["text"]
        for b in blocks
        if b["type"] == "section"
    ]


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------

class TestPrGrouping:
    def test_ready_to_merge_two_approvals_green_ci(self):
        pr = _pr(approval_count=2, approved_by=["tim", "shah"], ci_status="success")
        assert _groups({"prs": [pr]}) == ["READY TO MERGE"]

    def test_ready_to_merge_green_ci_no_ci_configured(self):
        pr = _pr(approval_count=2, approved_by=["tim", "shah"], ci_status="none")
        assert _groups({"prs": [pr]}) == ["READY TO MERGE"]

    def test_ci_failing_overrides_two_approvals(self):
        pr = _pr(approval_count=2, approved_by=["tim", "shah"], ci_status="failure")
        assert _groups({"prs": [pr]}) == ["CI FAILING"]

    def test_ci_failing_zero_approvals(self):
        pr = _pr(approval_count=0, approved_by=[], ci_status="failure")
        assert _groups({"prs": [pr]}) == ["CI FAILING"]

    def test_approved_one_review(self):
        pr = _pr(approval_count=1, approved_by=["tim"], ci_status="success")
        assert _groups({"prs": [pr]}) == ["APPROVED (1 review)"]

    def test_approved_one_review_pending_ci(self):
        pr = _pr(approval_count=1, approved_by=["tim"], ci_status="pending")
        assert _groups({"prs": [pr]}) == ["APPROVED (1 review)"]

    def test_stale_no_approvals(self):
        pr = _pr(approval_count=0, approved_by=[], age_days=6.0, ci_status="none")
        assert _groups({"prs": [pr]}) == ["STALE / AT RISK (>5d)"]

    def test_needs_review(self):
        pr = _pr(approval_count=0, approved_by=[], age_days=2.0, ci_status="none")
        assert _groups({"prs": [pr]}) == ["NEEDS REVIEW (>1d)"]

    def test_in_review(self):
        pr = _pr(approval_count=0, approved_by=[], age_days=0.5, ci_status="none")
        assert _groups({"prs": [pr]}) == ["IN REVIEW"]

    def test_group_order_ci_failing_first(self):
        prs = [
            _pr(id="1", approval_count=2, approved_by=["x", "y"], ci_status="success"),
            _pr(id="2", approval_count=0, ci_status="failure"),
        ]
        groups = _groups({"prs": prs})
        assert groups.index("CI FAILING") < groups.index("READY TO MERGE")

    def test_backward_compat_no_approval_count(self):
        """PRs from old pr_queue.json without approval_count use len(approved_by)."""
        pr = _pr(approved_by=["tim", "shah"])
        del pr["approval_count"]
        assert _groups({"prs": [pr]}) == ["READY TO MERGE"]

    def test_approved_two_waiting_on_ci(self):
        """2+ approvals with pending CI gets its own group, not READY TO MERGE."""
        pr = _pr(approval_count=2, approved_by=["tim", "shah"], ci_status="pending")
        assert _groups({"prs": [pr]}) == ["APPROVED — WAITING ON CI"]

    def test_approved_two_waiting_on_ci_before_approved_one(self):
        """APPROVED — WAITING ON CI sorts after READY TO MERGE but before APPROVED (1 review)."""
        prs = [
            _pr(id="1", approval_count=2, approved_by=["x", "y"], ci_status="pending"),
            _pr(id="2", approval_count=1, approved_by=["x"], ci_status="success"),
        ]
        groups = _groups({"prs": prs})
        assert groups.index("APPROVED — WAITING ON CI") < groups.index("APPROVED (1 review)")


# ---------------------------------------------------------------------------
# CI emoji in PR line
# ---------------------------------------------------------------------------

class TestCiEmojiDisplay:
    def test_failure_emoji_in_line(self):
        pr = _pr(ci_status="failure")
        lines = _lines({"prs": [pr]})
        assert any("❌" in line for line in lines)

    def test_success_emoji_in_line(self):
        pr = _pr(ci_status="success")
        lines = _lines({"prs": [pr]})
        assert any("✅" in line for line in lines)

    def test_pending_emoji_in_line(self):
        pr = _pr(ci_status="pending")
        lines = _lines({"prs": [pr]})
        assert any("⏳" in line for line in lines)

    def test_no_emoji_when_ci_none(self):
        pr = _pr(ci_status="none")
        lines = _lines({"prs": [pr]})
        assert not any(line.startswith("✅") or line.startswith("❌") or line.startswith("⏳") for line in lines)


# ---------------------------------------------------------------------------
# Approved-by display — actual names, not synthetic "2 approved" string
# ---------------------------------------------------------------------------

class TestApprovedByDisplay:
    def test_actual_names_shown(self):
        pr = _pr(approval_count=2, approved_by=["Tim", "Shahzad"], ci_status="success")
        blocks = build_pr_blocks("Test", {"prs": [pr]})
        field_texts = [
            f["text"]
            for b in blocks if b["type"] == "section"
            for f in b.get("fields", [])
        ]
        assert any("@Tim" in t and "@Shahzad" in t for t in field_texts)

    def test_no_synthetic_approved_string(self):
        pr = _pr(approval_count=2, approved_by=["Tim", "Shahzad"], ci_status="success")
        blocks = build_pr_blocks("Test", {"prs": [pr]})
        field_texts = " ".join(
            f["text"]
            for b in blocks if b["type"] == "section"
            for f in b.get("fields", [])
        )
        assert "2 approved" not in field_texts
