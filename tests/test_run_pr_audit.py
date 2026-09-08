"""Tests for scripts/analyze/run_pr_audit.py"""

import os
from unittest.mock import patch

import pytest

from scripts.analyze.run_pr_audit import _parse_slack_flags, _PR_AUDIT_PROMPT_TEMPLATE


class TestParseSlackFlags:
    def test_empty_message(self):
        config = {}
        n_prs, focus = _parse_slack_flags(config)
        assert n_prs is None
        assert focus is None

    def test_natural_language_prs(self):
        config = {"SLACK_MESSAGE": "audit last 30 prs"}
        n_prs, focus = _parse_slack_flags(config)
        assert n_prs == 30
        assert focus is None

    def test_flag_style_prs(self):
        config = {"SLACK_MESSAGE": "--n-prs 100"}
        n_prs, focus = _parse_slack_flags(config)
        assert n_prs == 100

    def test_focus_skills(self):
        config = {"SLACK_MESSAGE": "pr audit focus skills"}
        n_prs, focus = _parse_slack_flags(config)
        assert focus == "skills"

    def test_focus_flag(self):
        config = {"SLACK_MESSAGE": "--focus design"}
        n_prs, focus = _parse_slack_flags(config)
        assert focus == "design"

    def test_combined(self):
        config = {"SLACK_MESSAGE": "audit last 25 prs focus practices"}
        n_prs, focus = _parse_slack_flags(config)
        assert n_prs == 25
        assert focus == "practices"

    def test_env_fallback(self):
        with patch.dict(os.environ, {"SLACK_MESSAGE": "last 10 prs"}):
            config = {}
            n_prs, focus = _parse_slack_flags(config)
            assert n_prs == 10


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
