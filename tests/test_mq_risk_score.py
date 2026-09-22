"""Tests for scripts/mq/risk_score.py"""

import json
from pathlib import Path

import pytest

from scripts.mq.risk_score import (
    _is_test_file,
    _score_file_count,
    _score_historical,
    _score_sensitive_paths,
    _score_size,
    _score_test_coverage,
    _tier_for_score,
)


class TestScoreSize:
    def test_tiny(self):
        assert _score_size(5, 5) == 1

    def test_small(self):
        assert _score_size(30, 10) == 2

    def test_medium(self):
        assert _score_size(60, 30) == 3

    def test_large(self):
        assert _score_size(100, 50) == 5

    def test_very_large(self):
        assert _score_size(300, 100) == 7

    def test_huge(self):
        assert _score_size(500, 300) == 9

    def test_massive(self):
        assert _score_size(2000, 1000) == 10


class TestScoreFileCount:
    def test_few(self):
        assert _score_file_count(2) == 1

    def test_moderate(self):
        assert _score_file_count(7) == 4

    def test_many(self):
        assert _score_file_count(15) == 6

    def test_very_many(self):
        assert _score_file_count(30) == 8

    def test_extreme(self):
        assert _score_file_count(60) == 10


class TestScoreSensitivePaths:
    def test_no_sensitive(self):
        files = [{"path": "frontend/app.ts"}, {"path": "docs/guide.md"}]
        assert _score_sensitive_paths(files) == 0

    def test_one_sensitive(self):
        files = [{"path": "auth/login.py"}, {"path": "frontend/app.ts"}]
        assert _score_sensitive_paths(files) == 5

    def test_many_sensitive(self):
        files = [
            {"path": "auth/login.py"},
            {"path": "auth/tokens.py"},
            {"path": "security/rbac.py"},
            {"path": "billing/payments.py"},
        ]
        assert _score_sensitive_paths(files) == 7


class TestIsTestFile:
    def test_python_test(self):
        assert _is_test_file("test_foo.py")

    def test_go_test(self):
        assert _is_test_file("foo_test.go")

    def test_ts_test(self):
        assert _is_test_file("foo.test.ts")

    def test_java_test(self):
        assert _is_test_file("FooTest.java")

    def test_source_file(self):
        assert not _is_test_file("foo.py")

    def test_tests_dir(self):
        assert _is_test_file("src/tests/utils.py")


class TestScoreTestCoverage:
    def test_good_coverage(self):
        files = [
            {"path": "src/foo.py"},
            {"path": "tests/test_foo.py"},
        ]
        assert _score_test_coverage(files) == 0

    def test_no_tests(self):
        files = [{"path": "src/foo.py"}, {"path": "src/bar.py"}]
        assert _score_test_coverage(files) == 8

    def test_only_tests(self):
        files = [{"path": "tests/test_foo.py"}]
        assert _score_test_coverage(files) == 0


class TestScoreHistorical:
    def test_no_history_file(self, tmp_path):
        assert _score_historical(tmp_path) == 3

    def test_low_defect_rate(self, tmp_path):
        (tmp_path / "defect_history.json").write_text(
            json.dumps({"recent_defect_rate": 0.005})
        )
        assert _score_historical(tmp_path) == 1

    def test_high_defect_rate(self, tmp_path):
        (tmp_path / "defect_history.json").write_text(
            json.dumps({"recent_defect_rate": 0.15})
        )
        assert _score_historical(tmp_path) == 9

    def test_corrupt_file(self, tmp_path):
        (tmp_path / "defect_history.json").write_text("not json")
        assert _score_historical(tmp_path) == 3


class TestTierForScore:
    def test_low(self):
        assert _tier_for_score(10) == "LOW"

    def test_medium(self):
        assert _tier_for_score(20) == "MEDIUM"

    def test_high(self):
        assert _tier_for_score(40) == "HIGH"

    def test_boundary_low(self):
        assert _tier_for_score(15) == "LOW"

    def test_boundary_medium(self):
        assert _tier_for_score(30) == "MEDIUM"
