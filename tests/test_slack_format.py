"""Tests for scripts/common/slack_format.py — artifact link helpers."""

from scripts.common.slack_format import build_artifact_links, format_for_slack


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
