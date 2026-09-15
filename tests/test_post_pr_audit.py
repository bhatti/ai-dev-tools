"""Tests for scripts/analyze/post_pr_audit.py — Slack body selection logic."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _run_post(tmp_path: Path, config_overrides: dict, *, has_summary: bool = True) -> dict:
    """Run post_pr_audit.main() in isolation and return the result JSON."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()

    full_text = "## PR Audit\n### Executive Summary\nFull report content.\n### Critical Findings\n..."
    (reports_dir / "pr_audit_report.md").write_text(full_text, encoding="utf-8")

    summary_text = "### Executive Summary\nDigest content.\n### Top Findings\n• ...\n"
    if has_summary:
        (reports_dir / "slack_summary.md").write_text(summary_text, encoding="utf-8")

    findings = {"spec_gap_count": 2, "design_gap_count": 1, "skill_gap_count": 3,
                "practice_gap_count": 0, "repo": "org/repo", "branch": "main", "prs_analyzed": 20}
    (reports_dir / "pr_audit_findings.json").write_text(json.dumps(findings), encoding="utf-8")

    base_config = {"WORKSPACE_DIR": str(tmp_path), "SLACK_BOT_TOKEN": "", "SLACK_CHANNEL": "C0"}
    config = {**base_config, **config_overrides}

    posted_texts: list[str] = []

    def fake_post_report(cfg, slack_text, md_text, title, filename,
                         thread_ts=None, channel=None, task_type="post"):
        posted_texts.append(slack_text)
        assert full_text in md_text, "HTML attachment must always use full report"
        assert task_type == "audit-prs", f"Expected task_type='audit-prs', got '{task_type}'"
        return True

    with patch("scripts.analyze.post_pr_audit.load_config", return_value=config), \
         patch("scripts.analyze.post_pr_audit.post_report", side_effect=fake_post_report):
        import scripts.analyze.post_pr_audit as m
        m.main()

    return {"posted_text": posted_texts[0] if posted_texts else "", "summary_text": summary_text,
            "full_text": full_text}


class TestPostPrAuditSlackBodySelection:
    def test_summary_used_by_default(self, tmp_path):
        result = _run_post(tmp_path, {}, has_summary=True)
        assert "Digest content" in result["posted_text"]
        assert "Full report content" not in result["posted_text"]

    def test_full_flag_uses_full_report(self, tmp_path):
        result = _run_post(tmp_path, {"AUDIT_FULL_REPORT": "1"}, has_summary=True)
        assert "Full report content" in result["posted_text"]

    def test_full_flag_via_slack_message(self, tmp_path):
        """Slack path: SLACK_MESSAGE contains --full; AUDIT_FULL_REPORT is empty (subprocess env dies)."""
        result = _run_post(tmp_path, {"SLACK_MESSAGE": "pr-audit --full", "AUDIT_FULL_REPORT": ""}, has_summary=True)
        assert "Full report content" in result["posted_text"]

    def test_full_flag_slack_message_case_insensitive(self, tmp_path):
        result = _run_post(tmp_path, {"SLACK_MESSAGE": "PR-AUDIT --FULL"}, has_summary=True)
        assert "Full report content" in result["posted_text"]

    def test_no_full_in_slack_message_uses_digest(self, tmp_path):
        result = _run_post(tmp_path, {"SLACK_MESSAGE": "pr-audit last 20 prs"}, has_summary=True)
        assert "Digest content" in result["posted_text"]

    def test_fallback_when_no_summary(self, tmp_path):
        result = _run_post(tmp_path, {}, has_summary=False)
        assert "Full report content" in result["posted_text"]

    def test_html_always_uses_full_report(self, tmp_path):
        # Verified inside fake_post_report — if md_text is not the full report it raises.
        _run_post(tmp_path, {}, has_summary=True)

    def test_full_flag_html_still_full(self, tmp_path):
        _run_post(tmp_path, {"AUDIT_FULL_REPORT": "1"}, has_summary=True)

    def test_result_json_written(self, tmp_path):
        _run_post(tmp_path, {}, has_summary=True)
        result_path = tmp_path / "reports" / "post_pr_audit_result.json"
        assert result_path.exists()
        data = json.loads(result_path.read_text())
        assert "report_bytes" in data
        assert "slack_bytes" in data
        assert data["slack_posted"] is True

    def test_artifact_link_uses_by_job_endpoint(self, tmp_path):
        result = _run_post(tmp_path, {
            "FORMICARY_PUBLIC_URL": "https://formicary.example.com",
            "JOB_ID": "job-pr-audit-001",
        }, has_summary=True)
        text = result["posted_text"]
        assert "by-job/job-pr-audit-001/download" in text
        assert "task=audit-prs" in text
        assert "file=reports/pr_audit_report.html" in text
        assert "dashboard/jobs/requests/job-pr-audit-001" in text

    def test_artifact_link_absent_without_config(self, tmp_path):
        result = _run_post(tmp_path, {}, has_summary=True)
        assert "by-job" not in result["posted_text"]
