"""Tests for scripts/common/slack_format.py — artifact link helpers."""

from scripts.common.slack_format import build_artifact_links, format_for_slack, strip_section_heading


class TestBuildArtifactLinks:
    def test_builds_correct_urls(self):
        config = {
            "FORMICARY_PUBLIC_URL": "https://formicary.example.com",
            "JOB_ID": "job-123",
        }
        html_url, job_url = build_artifact_links(config, "audit-prs", "pr_audit_report.html")
        assert html_url == (
            "https://formicary.example.com/dashboard/artifacts/by-job/job-123"
            "/download?task=audit-prs&file=reports/pr_audit_report.html"
        )
        assert job_url == "https://formicary.example.com/dashboard/jobs/requests/job-123"

    def test_empty_when_no_public_url(self):
        config = {"JOB_ID": "job-123"}
        html_url, job_url = build_artifact_links(config, "post", "report.html")
        assert html_url == ""
        assert job_url == ""

    def test_empty_when_no_job_id(self):
        config = {"FORMICARY_PUBLIC_URL": "https://formicary.example.com"}
        html_url, job_url = build_artifact_links(config, "post", "report.html")
        assert html_url == ""
        assert job_url == ""

    def test_empty_when_both_missing(self):
        html_url, job_url = build_artifact_links({}, "post", "report.html")
        assert html_url == ""
        assert job_url == ""

    def test_strips_trailing_slash(self):
        config = {
            "FORMICARY_PUBLIC_URL": "https://formicary.example.com/",
            "JOB_ID": "job-456",
        }
        html_url, job_url = build_artifact_links(config, "audit", "audit_report.html")
        assert "formicary.example.com/dashboard/" in html_url
        assert "//" not in html_url.replace("https://", "")

    def test_different_task_types(self):
        config = {
            "FORMICARY_PUBLIC_URL": "https://f.io",
            "JOB_ID": "j1",
        }
        html_url, _ = build_artifact_links(config, "post", "report.html")
        assert "task=post" in html_url
        assert "file=reports/report.html" in html_url

        html_url2, _ = build_artifact_links(config, "audit-prs", "pr_audit_report.html")
        assert "task=audit-prs" in html_url2
        assert "file=reports/pr_audit_report.html" in html_url2

    def test_handles_none_values(self):
        config = {"FORMICARY_PUBLIC_URL": None, "JOB_ID": None}
        html_url, job_url = build_artifact_links(config, "post", "report.html")
        assert html_url == ""
        assert job_url == ""


class TestFormatForSlack:
    def test_converts_bold(self):
        assert "*hello*" in format_for_slack("**hello**")

    def test_truncates_long_text(self):
        long_text = "x" * 40_000
        result = format_for_slack(long_text)
        assert len(result) < 40_000
        assert "truncated" in result


class TestStripSectionHeading:
    def test_strips_risk_report_heading(self):
        text = "# Risk Report\n• item1\n• item2"
        assert strip_section_heading(text) == "• item1\n• item2"

    def test_strips_risk_report_with_sprint_info(self):
        text = "# Risk Report — DistMgmt Sprint 202 — 2026-09-18\n• item1"
        assert strip_section_heading(text) == "• item1"

    def test_strips_risk_report_h4(self):
        text = "#### RISK_REPORT\n• item"
        assert strip_section_heading(text) == "• item"

    def test_strips_standup_brief_heading(self):
        text = "## Standup Brief\n*Alice* — working on X"
        assert strip_section_heading(text) == "*Alice* — working on X"

    def test_strips_standup_brief_underscore(self):
        text = "#### STANDUP_BRIEF\n*Alice* — working on X"
        assert strip_section_heading(text) == "*Alice* — working on X"

    def test_preserves_body_headings(self):
        text = "• item1\n## Risk Report\n• item2"
        assert strip_section_heading(text) == text

    def test_noop_when_no_heading(self):
        text = "• item1\n• item2"
        assert strip_section_heading(text) == text

    def test_noop_on_unrelated_heading(self):
        text = "# Sprint Summary\n• item1"
        assert strip_section_heading(text) == "# Sprint Summary\n• item1"

    def test_strips_full_risk_report(self):
        text = "## Full Risk Report\n• risk1"
        assert strip_section_heading(text) == "• risk1"

    def test_empty_string(self):
        assert strip_section_heading("") == ""
