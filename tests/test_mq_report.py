"""Tests for scripts/mq/report.py"""

import json
import os
from pathlib import Path
from unittest.mock import patch

from scripts.mq.report import (
    _build_report, _generate_risk_heatmap_html, _read_json, _valley_of_calm_section,
)


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
        assert "Description" not in md  # removed for Slack verbosity
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
        assert "approval required" in md.lower()
        assert "risk score" in md
        assert ctx["GATE_APPROVAL"] == "true"

    def test_review_findings(self, tmp_path):
        (tmp_path / "review_result.json").write_text(json.dumps({
            "verdict": "DONE_WITH_CONCERNS",
            "findings": [
                {"severity": "critical", "category": "security",
                 "file": "auth/login.py", "line": 45,
                 "summary": "SQL injection via unsanitized input"},
                {"severity": "high", "category": "correctness",
                 "file": "api/handler.py", "line": 123,
                 "summary": "Off-by-one in pagination loop"},
                {"severity": "medium", "category": "test-coverage",
                 "file": "billing/charge.py", "line": 0,
                 "summary": "No tests for refund path"},
            ],
        }))
        md, ctx = _build_report(tmp_path, "", "ReviewFindings")
        assert "## Review Findings" in md
        assert "DONE_WITH_CONCERNS" in md
        assert "critical" in md
        assert "SQL injection" in md
        assert ctx["REVIEW_VERDICT"] == "DONE_WITH_CONCERNS"
        assert ctx["REVIEW_FINDINGS_COUNT"] == "3"
        assert ctx["REVIEW_CRITICAL"] == "1"
        assert ctx["REVIEW_HIGH"] == "1"

    def test_review_findings_with_gate(self, tmp_path):
        (tmp_path / "review_result.json").write_text(json.dumps({
            "verdict": "BLOCKED",
            "findings": [
                {"severity": "critical", "category": "security",
                 "file": "src/auth.py", "line": 10,
                 "summary": "Hardcoded secret in source"},
            ],
        }))
        (tmp_path / "gate_result.json").write_text(json.dumps({
            "needs_approval": True,
            "reason": "critical findings",
            "risk_score": 55.0,
            "risk_tier": "HIGH",
            "has_critical_findings": True,
            "findings_count": 1,
        }))
        md, ctx = _build_report(tmp_path, "", "ReviewGate")
        assert "## Review Findings" in md
        assert "## Gate Decision" in md
        assert "BLOCKED" in md
        assert "critical" in md
        assert "approval required" in md.lower()
        assert ctx["REVIEW_VERDICT"] == "BLOCKED"
        assert ctx["GATE_APPROVAL"] == "true"

    def test_lane_groups(self, tmp_path):
        (tmp_path / "lane_groups.json").write_text(json.dumps({
            "lanes": [
                {"lane_id": "main/low", "prs": [
                    {"pr_number": 1, "blast_radius": "low", "title": "billing fix",
                     "category": "backend", "pr_type": "bug", "age_hours": 5.0,
                     "ci_status": "none", "approval_count": 1, "reviewer_count": 2, "url": ""},
                    {"pr_number": 2, "blast_radius": "low", "title": "auth update",
                     "category": "api", "pr_type": "feature", "age_hours": 3.0,
                     "ci_status": "none", "approval_count": 0, "reviewer_count": 1, "url": ""},
                ]},
                {"lane_id": "dev/low", "prs": [
                    {"pr_number": 3, "blast_radius": "low", "title": "refactor",
                     "category": "backend", "pr_type": "unknown", "age_hours": 1.0,
                     "ci_status": "none", "approval_count": 0, "reviewer_count": 0, "url": ""},
                ]},
            ]
        }))
        md, ctx = _build_report(tmp_path, "", "Lanes")
        assert "**2 lanes**" in md
        assert "main" in md
        assert "#1" in md or "[#1]" in md
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


class TestContractTestReport:
    def test_renders_contract_summary(self, tmp_path):
        (tmp_path / "contract_test_summary.json").write_text(json.dumps({
            "pr_number": "42",
            "contract_breaking_changes": 0,
            "fuzz_iterations": 27,
            "fuzz_findings": 2,
            "critical_findings": 1,
            "endpoints_scanned": 9,
            "probe_types": ["SQLi", "path-traversal", "credential-exposure"],
            "status": "FAIL",
        }))
        md, ctx = _build_report(tmp_path, "42", "Contract Test")
        assert "Contract + Fuzz Security Testing" in md
        assert "FAIL" in md
        assert "❌" in md
        assert "27" in md
        assert "2" in md
        assert "9" in md  # endpoints scanned
        assert "SQLi" in md
        assert "credential-exposure" in md
        assert ctx["CONTRACT_STATUS"] == "FAIL"
        assert ctx["CONTRACT_FINDINGS"] == "2"
        assert ctx["CONTRACT_CRITICAL"] == "1"

    def test_renders_pass_status(self, tmp_path):
        (tmp_path / "contract_test_summary.json").write_text(json.dumps({
            "status": "PASS",
            "fuzz_iterations": 18,
            "fuzz_findings": 0,
            "critical_findings": 0,
            "contract_breaking_changes": 0,
        }))
        md, ctx = _build_report(tmp_path, "99", "Contract Test")
        assert "PASS" in md
        assert "✅" in md
        assert ctx["CONTRACT_STATUS"] == "PASS"

    def test_no_contract_section_when_file_absent(self, tmp_path):
        md, ctx = _build_report(tmp_path, "1", "Test")
        assert "Contract + Fuzz Security Testing" not in md
        assert "CONTRACT_STATUS" not in ctx


def _make_prs(n, pr_type="feature", ci_status="none", blast_radius="low",
              age_hours=2.0, risk_tier="low", is_hotspot=False):
    return [
        {
            "pr_number": i + 1,
            "blast_radius": blast_radius,
            "title": f"PR {i + 1}",
            "category": "backend",
            "pr_type": pr_type,
            "age_hours": age_hours,
            "ci_status": ci_status,
            "approval_count": 1,
            "reviewer_count": 1,
            "risk_tier": risk_tier,
            "risk_score": 10.0,
            "is_hotspot": is_hotspot,
            "author": "dev",
            "url": "",
        }
        for i in range(n)
    ]


class TestDeploymentRiskSection:
    def test_deployment_section_defaults_to_weekly_train(self):
        prs = _make_prs(10)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEPLOYMENT_PROFILE", None)
            result = _valley_of_calm_section(prs)
        assert "Deployment Risk Position" in result
        assert "weekly-train" in result

    def test_deployment_section_defaults_when_empty_profile(self):
        prs = _make_prs(10)
        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "  "}):
            result = _valley_of_calm_section(prs)
        assert "Deployment Risk Position" in result
        assert "weekly-train" in result

    def test_deployment_section_with_cd_profile(self):
        prs = _make_prs(8, pr_type="feature") + _make_prs(2, pr_type="bug")
        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "cd"}):
            result = _valley_of_calm_section(prs)
        assert "Deployment Risk Position" in result
        assert "cd" in result
        assert "Release cadence" in result
        assert "Release train success" in result
        assert "Rollback feasibility" in result
        assert "Deployment maturity" in result

    def test_deployment_section_with_weekly_train(self):
        prs = _make_prs(45, pr_type="feature") + _make_prs(5, pr_type="bug")
        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "weekly-train"}):
            result = _valley_of_calm_section(prs)
        assert "Deployment Risk Position" in result
        assert "weekly-train" in result
        assert "Releases stacked" in result

    def test_gauge_has_blue_marker(self):
        prs = _make_prs(10)
        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "cd"}):
            result = _valley_of_calm_section(prs)
        assert "🔵" in result

    def test_rollback_trap_warning_with_manual_profile(self):
        prs = _make_prs(10)
        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "manual"}):
            result = _valley_of_calm_section(prs)
        assert "Rollback trap" in result
        assert "roll-forward" in result.lower()

    def test_maturity_breakdown_table(self):
        prs = _make_prs(10)
        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "daily-train"}):
            result = _valley_of_calm_section(prs)
        assert "Deployment maturity breakdown" in result
        assert "Automated Testing" in result
        assert "Canary Deployment" in result
        assert "Observability" in result

    def test_high_defect_rate_red_status(self):
        prs = _make_prs(3, pr_type="feature") + _make_prs(7, pr_type="bug")
        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "weekly-train"}):
            result = _valley_of_calm_section(prs)
        assert "Calamity zone" in result or "Warning" in result

    def test_unknown_profile_no_deployment_section(self):
        prs = _make_prs(10)
        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "nonexistent-preset"}):
            result = _valley_of_calm_section(prs)
        assert "Deployment Risk Position" not in result

    def test_empty_prs_no_section(self):
        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "cd"}):
            result = _valley_of_calm_section([])
        assert result == ""

    def test_integration_via_build_report(self, tmp_path):
        prs = _make_prs(5, pr_type="feature") + _make_prs(1, pr_type="bug")
        (tmp_path / "lane_groups.json").write_text(json.dumps({
            "lanes": [{"lane_id": "main/low", "prs": prs}]
        }))
        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "cd"}):
            md, ctx = _build_report(tmp_path, "1", "Deploy Test")
        assert "Deployment Risk Position" in md

    def test_heatmap_appears_in_output(self):
        prs = _make_prs(8, pr_type="feature") + _make_prs(2, pr_type="bug")
        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "weekly-train"}):
            result = _valley_of_calm_section(prs)
        assert "Deployment Success Heatmap" in result
        assert "📍" in result

    def test_header_is_queue_health(self):
        prs = _make_prs(5)
        result = _valley_of_calm_section(prs)
        assert "## Queue Health" in result
        assert "Valley of Calm" not in result

    def test_health_labels_use_new_terminology(self):
        prs = _make_prs(10, age_hours=72.0)
        result = _valley_of_calm_section(prs)
        assert "Plateau of Misery" not in result
        assert "Degraded" not in result

    def test_stale_pr_table_has_status_column(self):
        prs = _make_prs(5, age_hours=400, is_hotspot=True)
        result = _valley_of_calm_section(prs)
        # Stale PR table now uses canonical columns from build_stale_pr_table()
        assert "| PR | Title | Author | Age | Status | Risk |" in result


class TestRiskHeatmapHtml:
    def _write_prs(self, tmp_path, n=20, bug_count=2):
        prs = _make_prs(n - bug_count, pr_type="feature") + _make_prs(bug_count, pr_type="bug")
        (tmp_path / "ready_prs.json").write_text(json.dumps(prs))
        return prs

    def test_generates_html_file(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        self._write_prs(tmp_path)
        _generate_risk_heatmap_html(tmp_path, reports)
        heatmap = reports / "risk_heatmap.html"
        assert heatmap.exists()
        html = heatmap.read_text()
        assert "Risk Heatmap" in html
        assert "Defect Rate" in html

    def test_heatmap_has_color_zones(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        self._write_prs(tmp_path)
        _generate_risk_heatmap_html(tmp_path, reports)
        html = (reports / "risk_heatmap.html").read_text()
        assert "#2ea043" in html  # green
        assert "#cf222e" in html  # red

    def test_heatmap_has_current_position(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        self._write_prs(tmp_path, n=20, bug_count=2)
        _generate_risk_heatmap_html(tmp_path, reports)
        html = (reports / "risk_heatmap.html").read_text()
        assert "Your position" in html

    def test_no_heatmap_when_no_prs_file(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        _generate_risk_heatmap_html(tmp_path, reports)
        assert not (reports / "risk_heatmap.html").exists()

    def test_no_heatmap_when_empty_prs(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (tmp_path / "ready_prs.json").write_text("[]")
        _generate_risk_heatmap_html(tmp_path, reports)
        assert not (reports / "risk_heatmap.html").exists()

    def test_heatmap_with_deployment_profile(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        self._write_prs(tmp_path, n=50, bug_count=5)
        with patch.dict(os.environ, {"DEPLOYMENT_PROFILE": "weekly-train"}):
            _generate_risk_heatmap_html(tmp_path, reports)
        html = (reports / "risk_heatmap.html").read_text()
        assert "PRs/release: 50" in html

    def test_heatmap_shows_summary_stats(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        self._write_prs(tmp_path, n=30, bug_count=3)
        _generate_risk_heatmap_html(tmp_path, reports)
        html = (reports / "risk_heatmap.html").read_text()
        assert "Queue size: 30" in html
        assert "Defect-proxy rate: 10.0%" in html
