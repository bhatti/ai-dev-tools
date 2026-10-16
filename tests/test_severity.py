"""Tests for scripts/common/severity.py."""

from __future__ import annotations

import pytest

from scripts.common import severity as sv


class TestCountFindings:
    def test_counts_by_level_case_insensitively(self):
        f = [{"severity": "CRITICAL"}, {"severity": "critical"}, {"severity": "High"}, {"severity": "MEDIUM"}, {"severity": "low"}]
        assert sv.count_findings(f) == {"critical": 2, "high": 1, "medium": 1, "low": 1, "total": 5, "major": 3}

    def test_vendor_words_fold_onto_our_scale(self):
        f = [{"severity": "BLOCKER"}, {"severity": "major"}, {"severity": "minor"}, {"severity": "info"}]
        c = sv.count_findings(f)
        assert (c["critical"], c["high"], c["low"], c["major"]) == (1, 1, 2, 2)

    def test_unknown_and_missing_severity_count_only_in_total(self):
        c = sv.count_findings([{"severity": "weird"}, {}])
        assert c["total"] == 2 and c["major"] == 0

    @pytest.mark.parametrize("empty", [None, []])
    def test_empty(self, empty):
        assert sv.count_findings(empty) == {"critical": 0, "high": 0, "medium": 0, "low": 0, "total": 0, "major": 0}


class TestCommentSeverity:
    @pytest.mark.parametrize("body,expected", [
        ("Severity: Critical — SQL injection", "CRITICAL"),
        ("[BLOCKER] leaks the token", "CRITICAL"),
        ("**Critical**: race in the cache", "CRITICAL"),
        ("🔴 Potential issue: nil deref", "CRITICAL"),
        ("🔴 Critical: leaks the token", "CRITICAL"),
        ("P0: data loss", "CRITICAL"),
        ("[P0] data loss", "CRITICAL"),
        ("Critical: this deletes rows", "CRITICAL"),
        ("## Major\nunbounded retry", "HIGH"),
        ("severity - high", "HIGH"),
        ("🟠 Major: missing auth check", "HIGH"),
        ("(P1) regression", "HIGH"),
        ("High - missing validation", "HIGH"),
        ("[high] timeout is 0", "HIGH"),
    ])
    def test_markers(self, body, expected):
        assert sv.comment_severity(body) == expected

    @pytest.mark.parametrize("body", [
        "High-level overview of the changes", "Looks good 🔴 CI is red", "see P1 ticket", "high-risk area, be careful", "🟠 build is slow",
        "LGTM", "", "this is on the critical path, fine", "high five", "a major refactor, nice", "nit: rename",
        "Looks good, maybe P2 follow-up", "criticality is low",
    ])
    def test_ordinary_prose_is_not_flagged(self, body):
        assert sv.comment_severity(body) == ""

    def test_critical_wins_over_high(self):
        assert sv.comment_severity("[critical] and also [high]") == "CRITICAL"
