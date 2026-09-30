"""Tests for scripts/analyze/run_codebase_audit.py"""

import os
from unittest.mock import patch

import pytest

from scripts.analyze.run_codebase_audit import _parse_slack_flags


class TestParseSlackFlags:
    def test_empty_message_returns_defaults(self):
        result = _parse_slack_flags({})
        assert result["n_commits"] is None
        assert result["max_size"] is None
        assert result["full_report"] is False

    def test_commits_natural_language(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit last 500 commits"})
        assert result["n_commits"] == 500

    def test_commits_flag(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "--commits 200"})
        assert result["n_commits"] == 200

    def test_max_size_mb(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit max size 2MB"})
        assert result["max_size"] == 2 * 1024 * 1024

    def test_max_size_kb(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit max size 512KB"})
        assert result["max_size"] == 512 * 1024

    def test_full_flag_detected(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "codebase-audit --full"})
        assert result["full_report"] is True

    def test_full_flag_absent(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "codebase-audit last 200 commits"})
        assert result["full_report"] is False

    def test_commits_and_full(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "last 500 commits --full"})
        assert result["n_commits"] == 500
        assert result["full_report"] is True

    def test_max_size_and_full(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit max size 2MB --full"})
        assert result["max_size"] == 2 * 1024 * 1024
        assert result["full_report"] is True

    def test_env_fallback(self):
        with patch.dict(os.environ, {"SLACK_MESSAGE": "last 100 commits"}):
            result = _parse_slack_flags({})
            assert result["n_commits"] == 100

    def test_returns_dict_not_tuple(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit"})
        assert isinstance(result, dict)
        assert set(result.keys()) == {"n_commits", "max_size", "full_report", "branch_override"}

    def test_target_flag(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "code-audit --target feature/my-branch"})
        assert result["branch_override"] == "feature/my-branch"

    def test_branch_flag_alias(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit --branch main"})
        assert result["branch_override"] == "main"

    def test_no_branch_override_returns_none(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit last 50 commits"})
        assert result["branch_override"] is None


class TestFetchPrsForMetrics:
    """_fetch_prs_for_metrics returns [] gracefully on any failure."""

    def test_returns_empty_list_on_missing_config(self):
        from scripts.analyze.run_codebase_audit import _fetch_prs_for_metrics
        # Missing config keys → fetch_prs raises KeyError / ValueError → returns []
        result = _fetch_prs_for_metrics({})
        assert isinstance(result, list)
        assert result == []

    def test_returns_empty_list_when_fetch_raises(self):
        from scripts.analyze.run_codebase_audit import _fetch_prs_for_metrics
        from unittest.mock import patch
        with patch("scripts.analyze.pr_fetcher.fetch_prs", side_effect=RuntimeError("no token")):
            result = _fetch_prs_for_metrics({})
        assert result == []

    def test_returns_prs_on_success(self):
        from scripts.analyze.run_codebase_audit import _fetch_prs_for_metrics
        from unittest.mock import patch
        fake_prs = [{"number": 1, "state": "merged"}, {"number": 2, "state": "open"}]
        with patch("scripts.analyze.pr_fetcher.fetch_prs", return_value=fake_prs):
            result = _fetch_prs_for_metrics({"GH_ORG": "org", "GH_REPO": "repo"})
        assert len(result) == 2


class TestAuditMetricsDashboardWithPRs:
    """Metrics dashboard in code-audit includes PR-level rows when PRs are available."""

    def _make_pr(self, **kw):
        defaults = {
            "state": "merged", "merged_at": "2026-09-01T10:00:00Z",
            "created_at": "2026-08-28T10:00:00Z", "total_loc": 150,
            "additions": 100, "deletions": 50, "size_bucket": "s",
            "blast_radius": "low", "ci_status": "pass",
            "has_substantive_review": True, "is_bot_authored": False,
            "rubber_stamp_approvers": [], "approvers": ["alice"],
            "is_hotspot": False, "is_wip_pr": False, "pr_type": "feature",
        }
        defaults.update(kw)
        return defaults

    def test_pr_state_breakdown_present_with_prs(self):
        from scripts.common.pr_classify import build_metrics_dashboard
        prs = [self._make_pr(number=i) for i in range(5)]
        audit_rows = [("Verbosity Ratio", "0.25", "<0.20", "🟡", "Comment+blank lines ratio")]
        result = build_metrics_dashboard(prs, extra_rows=audit_rows)
        assert "PR State Breakdown" in result
        assert "Verbosity Ratio" in result

    def test_review_coverage_present_with_prs(self):
        from scripts.common.pr_classify import build_metrics_dashboard
        prs = [self._make_pr(number=i) for i in range(3)]
        result = build_metrics_dashboard(prs, extra_rows=[])
        assert "Review Coverage" in result

    def test_audit_rows_still_present_with_empty_prs(self):
        from scripts.common.pr_classify import build_metrics_dashboard
        audit_rows = [("Erosion Score", "0.42", "<0.40", "🟡", "Code degradation signal")]
        result = build_metrics_dashboard([], extra_rows=audit_rows)
        assert "Erosion Score" in result
        assert "PR State Breakdown" not in result
