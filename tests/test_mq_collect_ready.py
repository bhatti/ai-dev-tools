"""Tests for scripts/mq/collect_ready.py"""

import json
from datetime import datetime, timezone, timedelta

import pytest

from scripts.mq.collect_ready import _compute_age_hours, _infer_scope


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


class TestInferScope:
    def test_single_module(self):
        files = [{"path": "billing/charge.py"}, {"path": "billing/invoice.py"}]
        assert _infer_scope(files) == "billing"

    def test_src_prefix(self):
        files = [{"path": "src/auth/login.py"}, {"path": "src/auth/session.py"}]
        assert _infer_scope(files) == "src/auth"

    def test_multi_module(self):
        files = [{"path": "billing/a.py"}, {"path": "auth/b.py"}]
        assert _infer_scope(files) == "multi-module"

    def test_cross_scope(self):
        files = [
            {"path": "billing/a.py"},
            {"path": "auth/b.py"},
            {"path": "frontend/c.py"},
        ]
        assert _infer_scope(files) == "cross-scope"

    def test_single_file_root(self):
        files = [{"path": "README.md"}]
        assert _infer_scope(files) == "README.md"

    def test_empty_files(self):
        assert _infer_scope([]) == "unknown"
