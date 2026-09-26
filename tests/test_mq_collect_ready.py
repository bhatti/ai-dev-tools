"""Tests for scripts/mq/collect_ready.py"""

import json
from datetime import datetime, timezone, timedelta

import pytest

from scripts.mq.collect_ready import _compute_age_hours, _normalize_pr


class TestComputeAgeHours:
    def test_recent_pr(self):
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        age = _compute_age_hours(one_hour_ago)
        assert 0.9 < age < 1.2

    def test_old_pr(self):
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        age = _compute_age_hours(two_days_ago)
        assert 47 < age < 49

    def test_z_suffix(self):
        one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1))
        ts = one_hour_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
        age = _compute_age_hours(ts)
        assert 0.9 < age < 1.2

    def test_invalid_timestamp(self):
        assert _compute_age_hours("not-a-date") == 0.0

    def test_empty_string(self):
        assert _compute_age_hours("") == 0.0


class TestNormalizePr:
    """_normalize_pr handles differing field names between gather_gh and bb_helpers."""

    def test_github_pr(self):
        pr = {
            "number": 42,
            "title": "Fix billing bug",
            "author": "alice",
            "age_hours": 3.5,
            "headRefName": "fix/billing",
            "ci_status": "success",
            "has_approval": True,
            "url": "https://github.com/org/repo/pull/42",
            "labels": ["bugfix"],
        }
        result = _normalize_pr(pr, "org/repo")
        assert result["pr_number"] == 42
        assert result["author"] == "alice"
        assert result["age_hours"] == 3.5
        assert result["branch"] == "fix/billing"
        assert result["ci_status"] == "success"
        assert result["has_approval"] is True
        assert result["labels"] == ["bugfix"]

    def test_bitbucket_pr(self):
        pr = {
            "id": 99,
            "title": "BB feature",
            "author": "bob",
            "age_hours": 10.0,
            "branch": "feature/new",
        }
        result = _normalize_pr(pr, "ws/repo")
        assert result["pr_number"] == 99
        assert result["author"] == "bob"
        assert result["ci_status"] == "none"
        assert result["has_approval"] is False
        assert result["labels"] == []

    def test_dict_author_github_format(self):
        pr = {"number": 7, "author": {"login": "carol"}, "age_hours": 1.0}
        result = _normalize_pr(pr, "org/repo")
        assert result["author"] == "carol"

    def test_dict_author_bb_format(self):
        pr = {"id": 8, "author": {"display_name": "Dan Smith"}, "age_hours": 2.0}
        result = _normalize_pr(pr, "ws/repo")
        assert result["author"] == "Dan Smith"

    def test_approval_from_approval_count(self):
        pr = {"id": 5, "approval_count": 2, "age_hours": 1.0}
        result = _normalize_pr(pr, "ws/repo")
        assert result["has_approval"] is True

    def test_repo_fallback(self):
        pr = {"number": 1, "age_hours": 0.5}
        result = _normalize_pr(pr, "fallback/repo")
        assert result["repo"] == "fallback/repo"

    def test_pr_own_repo_takes_precedence(self):
        pr = {"number": 1, "repo": "own/repo", "age_hours": 0.5}
        result = _normalize_pr(pr, "fallback/repo")
        assert result["repo"] == "own/repo"

    def test_scope_always_unknown(self):
        """scope_router.py sets authoritative scope downstream; collect_ready always emits 'unknown'."""
        pr = {"number": 3, "age_hours": 1.0}
        result = _normalize_pr(pr, "org/repo")
        assert result["scope"] == "unknown"
