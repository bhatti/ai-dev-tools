"""Tests for scripts/mq/report.py"""

import json
from pathlib import Path

from scripts.mq.report import _build_report, _read_json


class TestReadJson:
    def test_reads_valid(self, tmp_path):
        f = tmp_path / "test.json"
        f.write_text('{"key": "value"}')
        assert _read_json(f) == {"key": "value"}

    def test_missing_file(self, tmp_path):
        assert _read_json(tmp_path / "missing.json") is None

    def test_invalid_json(self, tmp_path):
        f = tmp_path / "bad.json"
        f.write_text("not json")
        assert _read_json(f) is None


class TestBuildReport:
    def test_empty_workspace(self, tmp_path):
        md, ctx = _build_report(tmp_path, "42", "Test Report")
        assert "# Test Report — PR #42" in md
        assert ctx == {}

    def test_scope_only(self, tmp_path):
        (tmp_path / "scope.json").write_text(json.dumps({
            "scope": "billing",
            "blast_radius": "high",
            "changed_files": 12,
            "lines_changed": 450,
            "touches": ["auth/login.py"],
            "owners": ["@team-payments"],
        }))
        md, ctx = _build_report(tmp_path, "1", "Scope")
        assert "billing" in md
        assert "high" in md
        assert "Description" in md
        assert "can merge in dedicated lane" in md
        assert "security-sensitive" in md
        assert "auth/login.py" in md
        assert "auth/security/billing/infra" in md
        assert "@team-payments" in md
        assert ctx["SCOPE"] == "billing"
        assert ctx["BLAST_RADIUS"] == "high"

    def test_scope_cross_scope_description(self, tmp_path):
        (tmp_path / "scope.json").write_text(json.dumps({
            "scope": "cross-scope",
            "blast_radius": "medium",
            "changed_files": 5,
            "lines_changed": 100,
        }))
        md, ctx = _build_report(tmp_path, "2", "Scope")
        assert "must serialize" in md
        assert "test independently" in md

    def test_risk_score(self, tmp_path):
        (tmp_path / "risk_score.json").write_text(json.dumps({
            "tier": "HIGH",
            "score": 42.5,
            "requires_human_approval": True,
            "dimensions": {"size": 5, "sensitive_paths": 8},
            "weights": {"size": 1.5, "sensitive_paths": 2.5},
            "additions": 300,
            "deletions": 50,
            "changed_files": 10,
            "has_historical_data": False,
        }))
        md, ctx = _build_report(tmp_path, "", "Risk")
        assert "HIGH" in md
        assert "42.5" in md
        assert "Human approval required" in md
        assert "Evidence" in md
        assert "Description" in md
        assert "350 lines" in md
        assert "no sensitive files" in md
        assert ctx["RISK_TIER"] == "HIGH"
        assert ctx["RISK_SCORE"] == "42.5"

    def test_risk_score_with_evidence(self, tmp_path):
        (tmp_path / "scope.json").write_text(json.dumps({
            "scope": "api", "blast_radius": "medium",
            "changed_files": 4, "lines_changed": 54,
            "touches": ["auth/token.py", "security/rbac.py"],
        }))
        (tmp_path / "risk_score.json").write_text(json.dumps({
            "tier": "MEDIUM",
            "score": 25.5,
            "requires_human_approval": False,
            "dimensions": {
                "size": 3, "file_count": 2, "blast_radius": 5,
                "sensitive_paths": 5, "test_coverage": 4, "historical": 3,
            },
            "weights": {"size": 1.5, "file_count": 1.0, "blast_radius": 2.0,
                        "sensitive_paths": 2.5, "test_coverage": 1.5, "historical": 1.0},
            "additions": 40,
            "deletions": 14,
            "changed_files": 4,
            "scope": "api",
            "has_historical_data": False,
        }))
        md, ctx = _build_report(tmp_path, "99", "Full Risk")
        assert "54 lines (+40/−14)" in md
        assert "4 files changed" in md
        assert "blast=medium, scope=api" in md
        assert "2 sensitive files: auth/token.py, security/rbac.py" in md
        assert "no defect history available" in md
        assert "Score breakdown" in md
        assert "Requires human approval" not in md

    def test_risk_score_historical_with_data(self, tmp_path):
        (tmp_path / "risk_score.json").write_text(json.dumps({
            "tier": "LOW",
            "score": 10.0,
            "requires_human_approval": False,
            "dimensions": {"historical": 6},
            "weights": {"historical": 1.0},
            "additions": 10, "deletions": 5, "changed_files": 1,
            "has_historical_data": True,
        }))
        md, ctx = _build_report(tmp_path, "", "Hist")
        assert "from defect_history.json" in md
        assert "no defect history" not in md

    def test_test_impact(self, tmp_path):
        (tmp_path / "test_impact.json").write_text(json.dumps({
            "total_tests": 1200,
            "selected_tests": 180,
            "reduction_pct": 85.0,
            "shards": [{"shard_id": "0"}, {"shard_id": "1"}],
            "unmapped_files": ["config.yaml"],
        }))
        md, ctx = _build_report(tmp_path, "5", "Impact")
        assert "180" in md
        assert "1200" in md
        assert "85" in md
        assert "config.yaml" in md
        assert ctx["TEST_REDUCTION_PCT"] == "85"

    def test_test_summary(self, tmp_path):
        (tmp_path / "test_summary.json").write_text(json.dumps({
            "passed": 178,
            "failed": 2,
            "skipped": 0,
            "total": 180,
            "shards": 4,
            "wall_clock_s": 48.5,
            "status": "FAIL",
        }))
        md, ctx = _build_report(tmp_path, "", "Summary")
        assert "178" in md
        assert "2 failed" in md
        assert ctx["TEST_STATUS"] == "FAIL"
        assert ctx["TESTS_PASSED"] == "178"
        assert ctx["TESTS_FAILED"] == "2"

    def test_shard_results_aggregated(self, tmp_path):
        for i in range(3):
            (tmp_path / f"shard_result_{i}.json").write_text(json.dumps({
                "shard_id": str(i),
                "passed": 10,
                "failed": 0,
                "skipped": 1,
                "duration_s": 20 + i,
            }))
        md, ctx = _build_report(tmp_path, "", "Shards")
        assert "30" in md  # 10*3 passed
        assert ctx["TESTS_PASSED"] == "30"
        assert ctx["TEST_STATUS"] == "PASS"

    def test_shard_results_with_failures(self, tmp_path):
        for i in range(2):
            (tmp_path / f"shard_result_{i}.json").write_text(json.dumps({
                "shard_id": str(i),
                "passed": 8,
                "failed": 2,
                "skipped": 0,
                "duration_s": 15,
            }))
        md, ctx = _build_report(tmp_path, "", "Fail")
        assert ctx["TEST_STATUS"] == "FAIL"
        assert ctx["TESTS_FAILED"] == "4"
        assert ctx["TESTS_PASSED"] == "16"

    def test_gate_decision(self, tmp_path):
        (tmp_path / "gate_result.json").write_text(json.dumps({
            "needs_approval": True,
            "reason": "risk score 45 >= 30",
            "findings_count": 3,
        }))
        md, ctx = _build_report(tmp_path, "", "Gate")
        assert "Approval required" in md
        assert "risk score" in md
        assert ctx["GATE_APPROVAL"] == "true"

    def test_lane_groups(self, tmp_path):
        (tmp_path / "lane_groups.json").write_text(json.dumps({
            "lanes": [
                {"lane_id": "billing", "prs": [{"pr_number": 1}, {"pr_number": 2}]},
                {"lane_id": "auth", "prs": [{"pr_number": 3}]},
            ]
        }))
        md, ctx = _build_report(tmp_path, "", "Lanes")
        assert "2 scope lanes" in md or "**2**" in md
        assert "billing" in md
        assert ctx["LANE_COUNT"] == "2"
        assert ctx["QUEUED_PRS"] == "3"

    def test_full_pipeline(self, tmp_path):
        (tmp_path / "scope.json").write_text(json.dumps({
            "scope": "api", "blast_radius": "low",
            "changed_files": 3, "lines_changed": 40,
        }))
        (tmp_path / "risk_score.json").write_text(json.dumps({
            "tier": "LOW", "score": 8.5,
            "requires_human_approval": False, "dimensions": {},
        }))
        (tmp_path / "test_impact.json").write_text(json.dumps({
            "total_tests": 500, "selected_tests": 25,
            "reduction_pct": 95.0, "shards": [{"shard_id": "0"}],
            "unmapped_files": [],
        }))
        (tmp_path / "test_summary.json").write_text(json.dumps({
            "passed": 25, "failed": 0, "total": 25,
            "shards": 1, "wall_clock_s": 12.3, "status": "PASS",
        }))
        md, ctx = _build_report(tmp_path, "42", "Pipeline")
        assert "## Scope" in md
        assert "## Risk Score" in md
        assert "## Test Impact Analysis" in md
        assert "## Test Results" in md
        assert ctx["SCOPE"] == "api"
        assert ctx["RISK_TIER"] == "LOW"
        assert ctx["TEST_STATUS"] == "PASS"

    def test_shard_performance_table(self, tmp_path):
        for i, dur in enumerate([30.5, 15.2, 22.8]):
            (tmp_path / f"shard_result_{i}.json").write_text(json.dumps({
                "shard_id": str(i),
                "passed": 10,
                "failed": 0,
                "skipped": 1,
                "duration_s": dur,
                "status": "passed",
                "slow_tests": [
                    {"name": f"tests/test_{i}.py::test_slow", "duration_s": dur / 2},
                ],
            }))
        (tmp_path / "test_summary.json").write_text(json.dumps({
            "passed": 30, "failed": 0, "total": 30,
            "shards": 3, "wall_clock_s": 30.5, "status": "PASS",
        }))
        md, ctx = _build_report(tmp_path, "1", "Shard Test")
        assert "### Shard Performance" in md
        assert "Parallel speedup" in md
        assert "30.5s" in md
        assert "### Slowest Tests" in md
        assert "test_slow" in md

    def test_shard_performance_not_shown_for_single_shard(self, tmp_path):
        (tmp_path / "shard_result_0.json").write_text(json.dumps({
            "shard_id": "0", "passed": 5, "failed": 0, "skipped": 0,
            "duration_s": 10.0, "status": "passed", "slow_tests": [],
        }))
        (tmp_path / "test_summary.json").write_text(json.dumps({
            "passed": 5, "failed": 0, "total": 5,
            "shards": 1, "wall_clock_s": 10.0, "status": "PASS",
        }))
        md, ctx = _build_report(tmp_path, "1", "Single Shard")
        assert "### Shard Performance" not in md
