"""Tests for scripts/common/slack_format.py — artifact link helpers."""

from scripts.common.slack_format import build_artifact_links, format_for_slack, strip_section_heading


class TestBuildArtifactLinks:
    def test_builds_correct_urls(self):
        config = {
            "FORMICARY_PUBLIC_URL": "https://formicary.example.com",
            "JOB_ID": "job-123",
        }
        reports_url, job_url = build_artifact_links(config)
        assert reports_url == "https://formicary.example.com/dashboard/jobs/requests/job-123#reports"
        assert job_url == "https://formicary.example.com/dashboard/jobs/requests/job-123"

    def test_empty_when_no_public_url(self):
        config = {"JOB_ID": "job-123"}
        reports_url, job_url = build_artifact_links(config)
        assert reports_url == ""
        assert job_url == ""

    def test_empty_when_no_job_id(self):
        config = {"FORMICARY_PUBLIC_URL": "https://formicary.example.com"}
        reports_url, job_url = build_artifact_links(config)
        assert reports_url == ""
        assert job_url == ""

    def test_empty_when_both_missing(self):
        reports_url, job_url = build_artifact_links({})
        assert reports_url == ""
        assert job_url == ""

    def test_strips_trailing_slash(self):
        config = {
            "FORMICARY_PUBLIC_URL": "https://formicary.example.com/",
            "JOB_ID": "job-456",
        }
        reports_url, job_url = build_artifact_links(config)
        assert reports_url.endswith("#reports")
        assert "//" not in reports_url.replace("https://", "")

    def test_backwards_compat_extra_args_ignored(self):
        config = {
            "FORMICARY_PUBLIC_URL": "https://f.io",
            "JOB_ID": "j1",
        }
        # Old callers may still pass task_type and report_filename positionally — they are ignored
        reports_url, _ = build_artifact_links(config, "report.html", "post")
        assert reports_url == "https://f.io/dashboard/jobs/requests/j1#reports"

    def test_handles_none_values(self):
        config = {"FORMICARY_PUBLIC_URL": None, "JOB_ID": None}
        reports_url, job_url = build_artifact_links(config)
        assert reports_url == ""
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
        text = "# Risk Report — Team Sprint 202 — 2026-09-18\n• item1"
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


def test_a_link_title_stays_visible_in_slack():
    assert "<https://x/pull/7|#7 Fix the login>" in format_for_slack('[#7](https://x/pull/7 "Fix the login")')


def test_a_titled_link_keeps_the_title_visible_in_slack_and_a_pipe_in_it_keeps_the_table_row_intact():
    md = ('| Status | PR | Type |\n|---|---|---|\n'
          '| PENDING | [#12](https://x/pull/12 "feat: a \\| b") | bug |\n')
    out = format_for_slack(md)
    assert "*PR:* <https://x/pull/12|#12 feat: a | b>" in out and "*Type:* bug" in out


def test_a_long_title_is_clipped_in_slack():
    out = format_for_slack('[#1](https://x/1 "' + "w" * 100 + '")')
    assert out.startswith("<https://x/1|#1 ") and out.endswith("…>") and len(out) < 100


def test_slack_control_characters_in_a_title_cannot_end_the_link():
    out = format_for_slack('[#1](https://x/1 "a->b & <c>")')
    assert out == "<https://x/1|#1 a-&gt;b &amp; &lt;c&gt;>"
