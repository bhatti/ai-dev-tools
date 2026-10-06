"""Tests for scripts/analyze/post_pr_audit.py — Slack digest and body selection."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest


# -- Fixtures ------------------------------------------------------------------

_SAMPLE_REPORT_MD = """\
## PR Audit

### Executive Summary

The most critical shipped gap is that 2/6 merged PRs landed with zero review.
Additionally, 56% of PRs have been open longer than 7 days.

### Shipped Gaps (Merged PRs)

[PRACTICE] HIGH: XL Sync/Promo PRs Merged with Zero Human Review — PRs #45832, #45439

Evidence: PR #45832 was self-approved. PR #45439 had zero substantive comments.

### In-Flight Concerns (Open PRs)

[DESIGN] HIGH: Conflicting HB Socket-Fix Approaches — PRs #48611 vs #49274

Both PRs modify the same four files with divergent approaches.

### Positive Patterns

Alice Smith — Exceptional Review Depth on Bot PRs
Bob Jones — API Contract Precision on v2 Migration PRs

### Skills Assessment

Coding: Developing, Code Review: Developing, Testing: Developing,
SRE/Ops: Gap, Security: Gap, Architecture: Strong

### Recommended Skill Updates

• Update .claude/skills/bot-pr-review: add test norm enforcement
• Create .bot/AGENTS.md: scope-boundary check for bot PRs
• Create .claude/skills/security-review: auth/token/infra path review

### Metrics Dashboard

| Metric | Value | Benchmark | Signal |
|--------|-------|-----------|--------|
| Review coverage | 50% | ≥80% | :red_circle: |

### Single-PR Observations

Minor one-off items.

### Checked — No Issues Found

RBAC, feature flag leakage, circular deps — all clear.

### Claude-Assessed Process Metrics

Duplicate metrics table.

### Pre-Computed PR Metrics Summary

Category breakdown and per-PR table with 34 rows.

### Per-PR Metrics

Detailed per-PR table here.
"""

_SAMPLE_FINDINGS = {
    "spec_gap_count": 3, "design_gap_count": 3,
    "skill_gap_count": 2, "practice_gap_count": 4,
    "repo": "acme/acme-app", "branch": "dev",
    "prs_analyzed": 34,
    "date_from": "2026-08-06", "date_to": "2026-09-29",
    "jiras_reviewed": 100,
    "findings": [
        {"severity": "HIGH", "category": "practice", "title": "XL PRs merged with zero review",
         "pr_state": "merged", "prs": [45832, 45439]},
        {"severity": "MEDIUM", "category": "practice", "title": "Stale merged PRs",
         "pr_state": "merged", "prs": [44799, 45245]},
        {"severity": "HIGH", "category": "design", "title": "Conflicting HB socket-fix",
         "pr_state": "open", "prs": [48611, 49274]},
        {"severity": "HIGH", "category": "practice", "title": "Bot PR stale 56d",
         "pr_state": "open", "prs": [45619]},
    ],
    "skills_assessment": {
        "coding": "Developing", "review": "Developing", "testing": "Developing",
        "sre": "Gap", "security": "Gap", "architecture": "Strong",
    },
    "metrics": {
        "spec_coverage_pct": 87.0, "ci_catch_rate": 75.0,
        "code_review_skill_catch_rate": 40.0,
        "bot_finding_follow_through_rate": 75.0,
        "human_review_burden": 55.0,
        "security_review_invocation_rate": 0.0,
        "rubber_stamp_rate": 50.0,
        "revert_followup_rate": 0.0,
        "verbosity_accumulation_rate": 15.0,
        "complexity_creep_pr_count": 3,
        "large_pr_review_depth": 0.0,
        "xl_pr_review_coverage_pct": 33.3,
        "large_pr_human_comments_avg": 0.0,
        "pr_state_merged": 6, "pr_state_open": 17, "pr_state_declined": 11,
        "avg_pr_size_loc": 1616, "median_pr_size_loc": 250,
    },
}


def _setup_workspace(tmp_path: Path, findings: dict | None = None,
                     report_md: str | None = None) -> dict:
    """Create workspace with report files and return config."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(exist_ok=True)
    (reports_dir / "pr_audit_report.md").write_text(
        report_md or _SAMPLE_REPORT_MD, encoding="utf-8")
    if findings is not None:
        (reports_dir / "pr_audit_findings.json").write_text(
            json.dumps(findings), encoding="utf-8")
    return {"WORKSPACE_DIR": str(tmp_path), "SLACK_BOT_TOKEN": "", "SLACK_CHANNEL": "C0"}


def _run_post(tmp_path: Path, config_overrides: dict | None = None,
              findings: dict | None = None) -> dict:
    """Run main() and capture posted Slack text + md_text."""
    config = _setup_workspace(tmp_path, findings or _SAMPLE_FINDINGS)
    config.update(config_overrides or {})

    posted_slacks: list[str] = []
    posted_mds: list[str] = []

    def fake_post(cfg, slack_text, md_text, title, filename,
                  thread_ts=None, channel=None, task_type="post"):
        posted_slacks.append(slack_text)
        posted_mds.append(md_text)
        return True

    with patch("scripts.analyze.post_pr_audit.load_config", return_value=config), \
         patch("scripts.analyze.post_pr_audit.post_report", side_effect=fake_post):
        import scripts.analyze.post_pr_audit as m
        m.main()

    return {
        "slack_text": posted_slacks[0] if posted_slacks else "",
        "md_text": posted_mds[0] if posted_mds else "",
    }


# -- Tests: Digest vs Full Selection ------------------------------------------

class TestSlackBodySelection:
    def test_default_uses_digest(self, tmp_path):
        result = _run_post(tmp_path)
        text = result["slack_text"]
        assert "Per-PR Metrics" not in text
        assert "Checked" not in text or "No Issues Found" not in text
        assert "Claude-Assessed Process Metrics" not in text

    def test_full_flag_includes_more_content(self, tmp_path):
        result = _run_post(tmp_path, {"AUDIT_FULL_REPORT": "1"})
        text = result["slack_text"]
        assert "Executive Summary" in text or "executive" in text.lower()

    def test_full_flag_via_slack_message(self, tmp_path):
        result = _run_post(tmp_path, {"SLACK_MESSAGE": "pr-audit --full"})
        text = result["slack_text"]
        assert "Shipped Gaps" in text or "shipped" in text.lower()

    def test_full_still_drops_verbose_sections(self, tmp_path):
        result = _run_post(tmp_path, {"AUDIT_FULL_REPORT": "1"})
        text = result["slack_text"]
        assert "Per-PR Metrics" not in text
        assert "Pre-Computed PR Metrics" not in text


# -- Tests: Digest Content ----------------------------------------------------

class TestSlackDigest:
    def test_includes_executive_summary(self, tmp_path):
        result = _run_post(tmp_path)
        assert "zero review" in result["slack_text"].lower() or "merged PRs" in result["slack_text"]

    def test_includes_findings_from_json(self, tmp_path):
        result = _run_post(tmp_path)
        text = result["slack_text"]
        assert "XL PRs merged" in text or "#45832" in text
        assert "Conflicting HB" in text or "#48611" in text

    def test_separates_shipped_from_open(self, tmp_path):
        result = _run_post(tmp_path)
        text = result["slack_text"]
        assert "Shipped" in text or "Merged" in text
        assert "In-Flight" in text or "Open" in text

    def test_includes_skills_assessment(self, tmp_path):
        result = _run_post(tmp_path)
        text = result["slack_text"]
        assert "Developing" in text
        assert "Gap" in text
        assert "Strong" in text

    def test_includes_key_metrics(self, tmp_path):
        result = _run_post(tmp_path)
        text = result["slack_text"]
        assert "Rubber-stamp" in text or "rubber" in text.lower()
        assert "Security review" in text or "security" in text.lower()

    def test_includes_recommended_skill_updates(self, tmp_path):
        result = _run_post(tmp_path)
        text = result["slack_text"]
        assert "bot-pr-review" in text.lower() or "Skill Updates" in text

    def test_includes_positive_patterns(self, tmp_path):
        result = _run_post(tmp_path)
        text = result["slack_text"]
        assert "Alice" in text or "Positive" in text

    def test_digest_omits_verbose_sections(self, tmp_path):
        result = _run_post(tmp_path)
        text = result["slack_text"]
        assert "Per-PR Metrics" not in text
        assert "Claude-Assessed" not in text
        assert "Single-PR Observations" not in text


# -- Tests: Header ------------------------------------------------------------

class TestHeader:
    def test_header_has_repo_and_branch(self, tmp_path):
        result = _run_post(tmp_path)
        assert "acme/acme-app" in result["slack_text"]
        assert "@ dev" in result["slack_text"]

    def test_header_has_date_range(self, tmp_path):
        result = _run_post(tmp_path)
        assert "2026-08-06" in result["slack_text"]
        assert "2026-09-29" in result["slack_text"]

    def test_header_has_jira_count(self, tmp_path):
        result = _run_post(tmp_path)
        assert "100 Jira issues" in result["slack_text"]

    def test_header_has_gap_counts(self, tmp_path):
        result = _run_post(tmp_path)
        assert "3 spec" in result["slack_text"]
        assert "4 practice" in result["slack_text"]

    def test_artifact_link(self, tmp_path):
        result = _run_post(tmp_path, {
            "FORMICARY_PUBLIC_URL": "https://formicary.example.com",
            "JOB_ID": "job-123",
        })
        assert "dashboard/jobs/requests/job-123#reports" in result["slack_text"]


# -- Tests: HTML/MD Artifact Always Full ---------------------------------------

class TestHtmlArtifact:
    def test_html_always_has_full_report(self, tmp_path):
        result = _run_post(tmp_path)
        md = result["md_text"]
        assert "Single-PR Observations" in md
        assert "Checked" in md

    def test_html_has_metadata_header(self, tmp_path):
        result = _run_post(tmp_path)
        md = result["md_text"]
        assert "34 PRs analyzed" in md
        assert "3 spec | 3 design" in md


# -- Tests: Result JSON -------------------------------------------------------

class TestResultJson:
    def test_result_json_written(self, tmp_path):
        _run_post(tmp_path)
        result_path = tmp_path / "reports" / "post_pr_audit_result.json"
        assert result_path.exists()
        data = json.loads(result_path.read_text())
        assert data["status"] == "OK"
        assert data["slack_posted"] is True
        assert data["digest_mode"] == "digest"

    def test_full_mode_result(self, tmp_path):
        _run_post(tmp_path, {"AUDIT_FULL_REPORT": "1"})
        data = json.loads((tmp_path / "reports" / "post_pr_audit_result.json").read_text())
        assert data["digest_mode"] == "full"


# -- Tests: Edge Cases ---------------------------------------------------------

class TestEdgeCases:
    def test_no_findings_json(self, tmp_path):
        config = _setup_workspace(tmp_path, findings=None)
        posted: list[str] = []

        def fake_post(cfg, slack_text, md_text, title, filename,
                      thread_ts=None, channel=None, task_type="post"):
            posted.append(slack_text)
            return True

        with patch("scripts.analyze.post_pr_audit.load_config", return_value=config), \
             patch("scripts.analyze.post_pr_audit.post_report", side_effect=fake_post):
            import scripts.analyze.post_pr_audit as m
            m.main()

        assert posted
        assert "PR Audit" in posted[0]

    def test_empty_findings_list(self, tmp_path):
        findings = {**_SAMPLE_FINDINGS, "findings": [], "metrics": {}, "skills_assessment": {}}
        result = _run_post(tmp_path, findings=findings)
        assert "PR Audit" in result["slack_text"]

    def test_missing_report_exits_1(self, tmp_path):
        config = {"WORKSPACE_DIR": str(tmp_path), "SLACK_BOT_TOKEN": "", "SLACK_CHANNEL": "C0"}
        (tmp_path / "reports").mkdir()

        with patch("scripts.analyze.post_pr_audit.load_config", return_value=config):
            with pytest.raises(SystemExit) as exc:
                import scripts.analyze.post_pr_audit as m
                m.main()
            assert exc.value.code == 1
