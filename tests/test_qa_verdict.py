"""Tests for scripts/qa/verdict.py: failure kinds, tagging, exit codes."""

import json

import pytest

from scripts.qa import verdict


class TestFailureKind:
    @pytest.mark.parametrize("message,side,kind", [
        ("connect ECONNREFUSED 127.0.0.1:8080", "candidate", "environment"),
        ("HTTP 503 service unavailable", "candidate", "environment"),
        ("cluster not ready after 120s", "candidate", "environment"),
        ("Traceback (most recent call last): repro harness crashed", "candidate", "unverified"),
        ("cannot evaluate the response: not JSON harness", "baseline", "unverified"),
        ("Expected 200 but got 404 for /api/items", "candidate", "product"),
        ("assertion failed: total != 3", "baseline", "product"),
    ])
    def test_default_classification(self, message, side, kind):
        assert verdict.failure_kind(message, side) == kind

    def test_timeout_is_a_baseline_problem_but_a_product_problem_on_the_candidate(self):
        assert verdict.failure_kind("request timed out after 30s", "baseline") == "baseline"
        assert verdict.failure_kind("request timed out after 30s", "candidate") == "product"

    def test_unverified_beats_environment(self):
        assert verdict.failure_kind("harness could not connect: ECONNREFUSED", "candidate") == "unverified"

    def test_patterns_are_configurable_and_replace_the_defaults_per_kind(self):
        patterns = {"environment": [r"vpn is down"]}
        assert verdict.failure_kind("the vpn is down", "candidate", patterns) == "environment"
        assert verdict.failure_kind("ECONNREFUSED", "candidate", patterns) == "product"

    def test_load_patterns_from_a_file_and_reject_bad_regexes(self, tmp_path):
        f = tmp_path / "p.json"
        f.write_text(json.dumps({"environment": ["flaky dns"]}))
        assert verdict.load_patterns(str(f)) == {"environment": ["flaky dns"]}
        f.write_text(json.dumps({"environment": ["("]}))
        with pytest.raises(ValueError):
            verdict.load_patterns(str(f))
        assert verdict.load_patterns("") is None


class TestTag:
    def test_non_product_kinds_get_a_prefix_product_stays_plain(self):
        assert verdict.tag("boom", "environment").startswith("[Environment / infrastructure")
        assert verdict.tag("boom", "baseline").startswith("[Baseline not ready")
        assert verdict.tag("boom", "unverified").startswith("[Could not verify")
        assert verdict.tag("boom", "product") == "boom"


class TestExitCode:
    @pytest.mark.parametrize("readiness,fail_on,code", [
        ("Ready", "", 0), ("Blocked", "", 0), ("Not ready", "", 0),
        ("Ready", "blocked", 0), ("Blocked", "blocked", 1), ("Not ready", "blocked", 0),
        ("Not ready", "not-ready", 1), ("Blocked", "not-ready", 1), ("Ready with concerns", "not-ready", 0),
        ("Ready with concerns", "concerns", 1), ("Ready", "concerns", 0),
    ])
    def test_thresholds(self, readiness, fail_on, code):
        assert verdict.exit_code(readiness, fail_on) == code

    def test_unknown_threshold_is_an_error(self):
        with pytest.raises(ValueError):
            verdict.exit_code("Ready", "everything")


class TestContract:
    def test_exercised_lists_one_entry_per_executed_case_with_channel_and_proof(self):
        plan = {"cases": [{"id": "C1", "type": "API"}, {"id": "C2", "type": "UI"}, {"id": "C3", "type": "MANUAL"}]}
        outcomes = {"C1": {"outcome": "pass", "evidence": {"evidence": "e/a.txt"}}, "C2": {"outcome": "fail", "evidence": {"screenshot": "s/b.png"}},
                    "C3": {"outcome": "not-run", "evidence": None}}
        ex = verdict.exercised(plan, outcomes, "candidate")
        assert ex == [{"id": "C1", "side": "candidate", "channel": "http", "status": "pass", "proof": "e/a.txt"},
                      {"id": "C2", "side": "candidate", "channel": "ui", "status": "fail", "proof": "s/b.png"}]

    def test_problems_are_structured_capped_and_codes_stable(self):
        items = verdict.problems({"blockers": ["b1"], "problems": ["p1"], "concerns": ["c1"] * 100})
        assert items[0] == {"code": "blocker", "message": "b1"} and items[1] == {"code": "problem", "message": "p1"}
        assert len(items) == verdict.MAX_PROBLEMS
