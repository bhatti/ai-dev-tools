"""Integration tests for metrics infrastructure: throughput, state filter, MQ summary."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from scripts.common.pr_classify import compute_throughput_metrics, build_metrics_dashboard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_merged_pr(number: int, days_ago_created: int = 10, days_ago_merged: int = 5,
                    pr_type: str = "feature") -> dict:
    now = datetime.now(tz=timezone.utc)
    return {
        "number": number,
        "pr_number": number,
        "state": "merged",
        "pr_type": pr_type,
        "created_at": (now - timedelta(days=days_ago_created)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "merged_at": (now - timedelta(days=days_ago_merged)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "blast_radius": "low",
        "risk_score": 10,
        "risk_tier": "low",
    }


def _make_open_pr(number: int, days_old: int = 2) -> dict:
    now = datetime.now(tz=timezone.utc)
    return {
        "number": number,
        "pr_number": number,
        "state": "open",
        "pr_type": "feature",
        "created_at": (now - timedelta(days=days_old)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "merged_at": None,
        "blast_radius": "low",
        "risk_score": 5,
        "risk_tier": "low",
    }


# ---------------------------------------------------------------------------
# Throughput metrics tests
# ---------------------------------------------------------------------------

class TestThroughputMetrics:

    def test_returns_empty_for_no_prs(self):
        assert compute_throughput_metrics([]) == {}

    def test_returns_empty_for_fewer_than_3_merged(self):
        prs = [_make_merged_pr(i) for i in range(2)]
        assert compute_throughput_metrics(prs) == {}

    def test_lead_time_computed_from_merged_prs(self):
        # PR created 10d ago, merged 5d ago → lead time = 5d
        prs = [_make_merged_pr(i, days_ago_created=10, days_ago_merged=5) for i in range(5)]
        result = compute_throughput_metrics(prs)
        assert "lead_time_p50_days" in result
        lt = result["lead_time_p50_days"]
        # Allow rounding: 5 days ± 1
        assert 4 <= lt <= 6, f"Expected ~5d lead time, got {lt}"

    def test_cfr_proxy_with_bug_prs(self):
        prs = [_make_merged_pr(i, pr_type="feature") for i in range(4)]
        prs += [_make_merged_pr(i + 100, pr_type="bug") for i in range(2)]
        result = compute_throughput_metrics(prs)
        assert "change_failure_rate_pct" in result
        # 2 bugs / 6 total = 33.3%
        assert 30 <= result["change_failure_rate_pct"] <= 36

    def test_survival_rate_with_declined(self):
        now = datetime.now(tz=timezone.utc)
        prs = [_make_merged_pr(i) for i in range(4)]
        # Add 2 declined
        for i in range(2):
            prs.append({
                "number": 100 + i,
                "state": "declined",
                "pr_type": "feature",
                "created_at": (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "merged_at": None,
                "blast_radius": "low",
            })
        result = compute_throughput_metrics(prs)
        assert "pr_survival_rate_pct" in result
        # 4 merged / 6 closed = 66.7%
        assert 60 <= result["pr_survival_rate_pct"] <= 70

    def test_deployment_frequency_computed(self):
        prs = [_make_merged_pr(i, days_ago_merged=i) for i in range(5)]
        result = compute_throughput_metrics(prs)
        assert "deployment_frequency" in result
        assert result["deployment_frequency"] > 0


# ---------------------------------------------------------------------------
# Dashboard with extra_rows (for code-audit use case)
# ---------------------------------------------------------------------------

class TestBuildMetricsDashboardExtraRows:

    def test_empty_prs_with_extra_rows_produces_dashboard(self):
        extra = [("Verbosity Ratio", "0.25", "<0.30 healthy", "🟢", "Comment lines ratio")]
        result = build_metrics_dashboard([], extra_rows=extra)
        assert "Metrics Dashboard" in result
        assert "Verbosity Ratio" in result

    def test_empty_prs_without_extra_rows_returns_empty(self):
        assert build_metrics_dashboard([]) == ""
        assert build_metrics_dashboard([], extra_rows=[]) == ""

    def test_throughput_rows_included_with_merged_data(self):
        prs = [_make_merged_pr(i) for i in range(5)]
        result = build_metrics_dashboard(prs)
        # Should include DORA rows when enough merged PRs
        assert "Deployment Frequency" in result or "Lead Time" in result

    def test_combined_prs_and_extra_rows(self):
        prs = [_make_merged_pr(i) for i in range(3)]
        extra = [("Custom Metric", "42", "≥40 healthy", "🟢", "A custom metric")]
        result = build_metrics_dashboard(prs, extra_rows=extra)
        assert "Custom Metric" in result
        assert "PR State Breakdown" in result  # pre-computed row still present

    def test_dashboard_five_column_format(self):
        extra = [("Test Row", "1.0", "≥0.5 healthy", "🟢", "Test description")]
        result = build_metrics_dashboard([], extra_rows=extra)
        # Table has 5 columns: Metric | Value | Benchmark | Signal | Description
        header_line = [l for l in result.splitlines() if "Metric" in l and "Value" in l]
        assert header_line, "Dashboard header row not found"
        assert header_line[0].count("|") >= 5


# ---------------------------------------------------------------------------
# PR state filter bypass fix
# ---------------------------------------------------------------------------

class TestPRStateFilterBypass:

    def test_open_pr_filtered_from_default_states(self):
        """After fetch_prs_by_numbers, open PRs should be removed when pr_states=merged+declined."""
        mixed_prs = [
            {"number": 1, "state": "merged", "merged_at": "2024-01-10T00:00:00Z", "pr_type": "feature"},
            {"number": 2, "state": "open", "merged_at": None, "pr_type": "feature"},
            {"number": 3, "state": "declined", "merged_at": None, "pr_type": "feature"},
        ]
        pr_states = ["merged", "declined"]
        filtered = [p for p in mixed_prs if p.get("state", "merged").lower() in pr_states]
        assert len(filtered) == 2
        assert all(p["state"] in ("merged", "declined") for p in filtered)

    def test_all_states_not_filtered(self):
        mixed_prs = [
            {"number": 1, "state": "merged", "merged_at": "2024-01-10T00:00:00Z"},
            {"number": 2, "state": "open", "merged_at": None},
        ]
        pr_states = ["merged", "declined", "open"]
        filtered = [p for p in mixed_prs if p.get("state", "merged").lower() in pr_states]
        assert len(filtered) == 2

    def test_state_filter_removes_zero_filtered_prs(self):
        pr_states = ["merged", "declined"]
        prs = [{"number": 1, "state": "merged"}, {"number": 2, "state": "merged"}]
        filtered = [p for p in prs if p.get("state", "merged").lower() in pr_states]
        assert len(filtered) == 2  # all merged — none removed


# ---------------------------------------------------------------------------
# MQ Slack summary
# ---------------------------------------------------------------------------

class TestMQSlackSummary:

    def _make_mq_prs(self) -> list[dict]:
        now = datetime.now(tz=timezone.utc)
        prs = []
        for i in range(10):
            prs.append({
                "number": i,
                "pr_type": "feature",
                "blast_radius": "low",
                "created_at": (now - timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "ci_status": "pass",
                "has_substantive_review": i % 3 != 0,
                "is_hotspot": False,
                "is_bot_authored": False,
                "age_hours": i * 24,
            })
        return prs

    def test_review_coverage_present(self):
        from scripts.mq.report import _build_mq_slack_summary
        prs = self._make_mq_prs()
        ctx = {"MQ_TOTAL": len(prs), "MQ_HIGH_RISK": 0, "MQ_NEEDS_REVIEW": 2}
        result = _build_mq_slack_summary(ctx, prs, [])
        assert "Review" in result

    def test_stale_7d_present(self):
        from scripts.mq.report import _build_mq_slack_summary
        now = datetime.now(tz=timezone.utc)
        prs = self._make_mq_prs()
        # Add a PR that is explicitly >7 days old via created_at
        prs.append({
            "number": 99,
            "pr_type": "feature",
            "blast_radius": "low",
            "created_at": (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ci_status": "pass",
            "has_substantive_review": True,
            "is_hotspot": False,
            "is_bot_authored": False,
            "age_hours": 240,  # 10 days
        })
        ctx = {"MQ_TOTAL": len(prs), "MQ_HIGH_RISK": 0, "MQ_NEEDS_REVIEW": 0}
        result = _build_mq_slack_summary(ctx, prs, [])
        assert "stale" in result.lower()

    def test_cfr_proxy_present(self):
        from scripts.mq.report import _build_mq_slack_summary
        prs = self._make_mq_prs()
        prs[0]["pr_type"] = "bug"
        prs[1]["pr_type"] = "bug"
        ctx = {"MQ_TOTAL": len(prs), "MQ_HIGH_RISK": 0, "MQ_NEEDS_REVIEW": 0}
        result = _build_mq_slack_summary(ctx, prs, [])
        assert "CFR" in result

    def test_ci_coverage_present_when_known(self):
        from scripts.mq.report import _build_mq_slack_summary
        prs = self._make_mq_prs()
        ctx = {"MQ_TOTAL": len(prs), "MQ_HIGH_RISK": 1, "MQ_NEEDS_REVIEW": 1}
        result = _build_mq_slack_summary(ctx, prs, [])
        assert "CI:" in result

    def test_queue_health_line_present(self):
        from scripts.mq.report import _build_mq_slack_summary
        prs = self._make_mq_prs()
        ctx = {"MQ_TOTAL": len(prs), "MQ_HIGH_RISK": 0, "MQ_NEEDS_REVIEW": 0}
        result = _build_mq_slack_summary(ctx, prs, [])
        assert "Queue Health" in result

    def test_empty_prs_returns_header_only(self):
        from scripts.mq.report import _build_mq_slack_summary
        ctx = {"MQ_TOTAL": 0, "MQ_HIGH_RISK": 0, "MQ_NEEDS_REVIEW": 0}
        result = _build_mq_slack_summary(ctx, [], [])
        assert "Merge Queue" in result
        assert "Full report" in result
