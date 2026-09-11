"""Tests for scripts/analyze/run_pr_audit.py"""

import os
from unittest.mock import patch

import pytest

from scripts.analyze.run_pr_audit import _parse_slack_flags, _PR_AUDIT_PROMPT_TEMPLATE


class TestParseSlackFlags:
    def test_empty_message(self):
        result = _parse_slack_flags({})
        assert result["n_prs"] is None
        assert result["focus"] is None
        assert result["pr_urls"] == []
        assert result["model"] is None

    def test_natural_language_prs(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit last 30 prs"})
        assert result["n_prs"] == 30
        assert result["focus"] is None

    def test_flag_style_prs(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "--n-prs 100"})
        assert result["n_prs"] == 100

    def test_focus_skills(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr audit focus skills"})
        assert result["focus"] == "skills"

    def test_focus_flag(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "--focus design"})
        assert result["focus"] == "design"

    def test_combined(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit last 25 prs focus practices"})
        assert result["n_prs"] == 25
        assert result["focus"] == "practices"

    def test_env_fallback(self):
        with patch.dict(os.environ, {"SLACK_MESSAGE": "last 10 prs"}):
            result = _parse_slack_flags({})
            assert result["n_prs"] == 10

    def test_github_pr_url_extracted(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit https://github.com/org/repo/pull/42"})
        assert result["pr_urls"] == ["https://github.com/org/repo/pull/42"]

    def test_bitbucket_pr_url_extracted(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "review https://bitbucket.org/ws/repo/pull-requests/100"})
        assert result["pr_urls"] == ["https://bitbucket.org/ws/repo/pull-requests/100"]

    def test_multiple_pr_urls(self):
        msg = "audit https://github.com/org/repo/pull/1 https://github.com/org/repo/pull/2"
        result = _parse_slack_flags({"SLACK_MESSAGE": msg})
        assert len(result["pr_urls"]) == 2

    def test_model_flag(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit last 10 prs --model claude-opus-5"})
        assert result["model"] == "claude-opus-5"
        assert result["n_prs"] == 10

    def test_model_colon_syntax(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "model: us.anthropic.claude-opus-4-6-v1"})
        assert result["model"] == "us.anthropic.claude-opus-4-6-v1"

    def test_invalid_url_not_extracted(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit https://example.com/something"})
        assert result["pr_urls"] == []


class TestPromptTemplate:
    def test_template_renders(self):
        """Verify the prompt template can be formatted without KeyError."""
        result = _PR_AUDIT_PROMPT_TEMPLATE.format(
            repo_label="org/repo",
            branch="main",
            n_prs=50,
            focus="all",
            pr_context="## PRs\n(test data)",
            skill_instructions="Analyze the PRs.",
            pr_ids="1,2,3",
        )
        assert "org/repo" in result
        assert "50" in result
        assert "all" in result
        assert "Analyze the PRs." in result
        assert "pr_audit_report.md" in result
        assert "pr_audit_findings.json" in result
        assert "skill_improvements.json" in result

    def test_template_has_required_outputs(self):
        """Verify the template mentions all three required output files."""
        assert "pr_audit_report.md" in _PR_AUDIT_PROMPT_TEMPLATE
        assert "pr_audit_findings.json" in _PR_AUDIT_PROMPT_TEMPLATE
        assert "skill_improvements.json" in _PR_AUDIT_PROMPT_TEMPLATE
