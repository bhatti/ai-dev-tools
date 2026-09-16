"""Tests for scripts/analyze/run_pr_audit.py"""

import os
from unittest.mock import patch

import pytest

from scripts.analyze.run_pr_audit import (
    _parse_slack_flags,
    _PR_AUDIT_PROMPT_TEMPLATE,
    _resolve_effective_tracker,
    _resolve_branch,
)


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

    # --- team / board / milestone filtering ---

    def test_team_flag_dash_dash(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr audit --team alice,bob"})
        assert result["team_members"] == "alice,bob"

    def test_team_colon_syntax(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit team:alice,bob"})
        assert result["team_members"] == "alice,bob"

    def test_board_flag_with_id(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --board 123"})
        assert result["jira_boards"] == "123"

    def test_board_flag_no_id_returns_default_sentinel(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --board"})
        assert result["jira_boards"] == "__default__"

    def test_board_colon_syntax(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit board:123"})
        assert result["jira_boards"] == "123"

    def test_board_url_path(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "https://company.atlassian.net/jira/software/c/projects/PROJ/boards/789"})
        assert result["jira_boards"] == "789"

    def test_milestone_flag(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit --milestone v2.5"})
        assert result["gh_milestone"] == "v2.5"

    def test_empty_message_new_keys_present(self):
        result = _parse_slack_flags({})
        assert "team_members" in result
        assert "jira_boards" in result
        assert "gh_milestone" in result
        assert "jira_team" in result
        assert "pr_filter" in result
        assert result["team_members"] is None
        assert result["jira_boards"] is None
        assert result["gh_milestone"] is None
        assert result["jira_team"] is None
        assert result["pr_filter"] is None

    def test_jira_team_flag_single_word(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --team MyTeam last 20 prs"})
        assert result["jira_team"] == "MyTeam"
        assert result["team_members"] is None  # no comma → not a member list

    def test_team_with_comma_is_member_list_not_jira_team(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --team alice,bob"})
        assert result["team_members"] == "alice,bob"
        assert result["jira_team"] is None

    def test_filter_flag_simple(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --filter label=security"})
        assert result["pr_filter"] == "label=security"

    def test_filter_flag_with_quoted_field(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": 'pr-audit --filter "Eng Scrum Team"=TeamAlpha'})
        assert result["pr_filter"] == "Eng Scrum Team=TeamAlpha"

    def test_combined_board_and_jira_team(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --board 1234 --team MyTeam last 10 prs"})
        assert result["jira_boards"] == "1234"
        assert result["jira_team"] == "MyTeam"

    def test_board_without_team_leaves_jira_team_none(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --board 1234 last 10 prs"})
        assert result["jira_boards"] == "1234"
        assert result["jira_team"] is None

    def test_combined_team_and_milestone(self):
        # Single-word --team alice → jira_team (not team_members); use comma for member list
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit last 20 prs --team alice --milestone sprint-3"})
        assert result["n_prs"] == 20
        assert result["jira_team"] == "alice"
        assert result["team_members"] is None
        assert result["gh_milestone"] == "sprint-3"

    def test_tracker_flag_github(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --tracker github"})
        assert result["tracker"] == "github"

    def test_tracker_flag_jira(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --tracker jira"})
        assert result["tracker"] == "jira"

    def test_tracker_absent_returns_none(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --board 123"})
        assert result["tracker"] is None

    def test_empty_message_tracker_key_present(self):
        result = _parse_slack_flags({})
        assert "tracker" in result
        assert result["tracker"] is None

    def test_full_flag_detected(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --full"})
        assert result["full_report"] is True

    def test_full_flag_absent(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --board 123"})
        assert result["full_report"] is False

    def test_full_flag_case_insensitive(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "PR-AUDIT --FULL"})
        assert result["full_report"] is True

    def test_full_flag_combined_with_other_flags(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit last 20 prs --full --tracker github"})
        assert result["full_report"] is True
        assert result["n_prs"] == 20
        assert result["tracker"] == "github"

    def test_empty_message_full_report_key_present(self):
        result = _parse_slack_flags({})
        assert "full_report" in result
        assert result["full_report"] is False


class TestResolveEffectiveTracker:
    """_resolve_effective_tracker priority: --tracker > PR URL > DEFAULT_TRACKER."""

    def test_explicit_tracker_wins_over_url_and_default(self):
        flags = {"tracker": "jira", "pr_urls": ["https://github.com/org/repo/pull/1"]}
        assert _resolve_effective_tracker({"DEFAULT_TRACKER": "github"}, flags) == "jira"

    def test_pr_url_wins_over_default(self):
        flags = {"tracker": None, "pr_urls": ["https://github.com/org/repo/pull/1"]}
        assert _resolve_effective_tracker({"DEFAULT_TRACKER": "jira"}, flags) == "github"

    def test_default_tracker_used_when_no_flag_no_url(self):
        flags = {"tracker": None, "pr_urls": []}
        assert _resolve_effective_tracker({"DEFAULT_TRACKER": "jira"}, flags) == "jira"

    def test_empty_config_returns_empty_string(self):
        flags = {"tracker": None, "pr_urls": []}
        assert _resolve_effective_tracker({}, flags) == ""

    def test_bitbucket_url_derives_jira_tracker(self):
        flags = {"tracker": None, "pr_urls": ["https://bitbucket.org/ws/repo/pull-requests/42"]}
        result = _resolve_effective_tracker({"DEFAULT_TRACKER": "github"}, flags)
        assert result in ("jira", "jira/bitbucket", "bitbucket")


class TestResolveBranch:
    """_resolve_branch picks the right branch env var for the effective tracker."""

    def test_github_tracker_uses_gh_branch(self):
        config = {"GH_REPO_BRANCH": "feature-x", "BB_REPO_BRANCH": "develop"}
        assert _resolve_branch(config, "github") == "feature-x"

    def test_jira_tracker_uses_bb_branch(self):
        config = {"GH_REPO_BRANCH": "main", "BB_REPO_BRANCH": "develop"}
        assert _resolve_branch(config, "jira") == "develop"

    def test_bitbucket_tracker_uses_bb_branch(self):
        config = {"GH_REPO_BRANCH": "main", "BB_REPO_BRANCH": "release"}
        assert _resolve_branch(config, "bitbucket") == "release"

    def test_missing_branch_defaults_to_main(self):
        assert _resolve_branch({}, "github") == "main"
        assert _resolve_branch({}, "jira") == "main"

    def test_has_bitbucket_creds_but_tracker_github_uses_gh_branch(self):
        config = {
            "BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "repo",
            "GH_REPO_BRANCH": "gh-branch", "BB_REPO_BRANCH": "bb-branch",
        }
        assert _resolve_branch(config, "github") == "gh-branch"


class TestGhTrackerBoardWarning:
    """--board with a GitHub-tracker job warns and skips when Jira creds absent; allows when present."""

    def test_board_flag_parsed_correctly(self):
        import scripts.analyze.run_pr_audit as m
        flags = m._parse_slack_flags({"SLACK_MESSAGE": "pr-audit --board 456"})
        assert flags["jira_boards"] == "456"

    def test_board_flag_without_id_returns_sentinel(self):
        import scripts.analyze.run_pr_audit as m
        flags = m._parse_slack_flags({"SLACK_MESSAGE": "pr-audit --board"})
        assert flags["jira_boards"] == "__default__"


class TestMainEnvWrites:
    """Verify main() propagates Slack flags to os.environ for downstream tasks.

    main() exits early (sys.exit(1)) when no repo URL or CODEBASE_DIR is provided.
    The env writes happen before that exit, so catching SystemExit is correct here.
    """

    def _run_main_with_message(self, message: str, monkeypatch, tmp_path, extra_env=None):
        monkeypatch.setenv("SLACK_MESSAGE", message)
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
        monkeypatch.setenv("ANTHROPIC_BEDROCK_BASE_URL", "http://ai/bedrock")
        monkeypatch.setenv("BITBUCKET_WORKSPACE", "ws")
        monkeypatch.setenv("BITBUCKET_REPO", "repo")
        monkeypatch.delenv("CODEBASE_DIR", raising=False)
        for k, v in (extra_env or {}).items():
            monkeypatch.setenv(k, v)

        from scripts.analyze import run_pr_audit
        with patch("scripts.analyze.run_pr_audit.validate_claude_config"), \
             patch("scripts.analyze.run_pr_audit._ensure_ygs_skills"), \
             patch("scripts.analyze.run_pr_audit.resolve_repo_url", return_value=None), \
             patch("scripts.analyze.run_pr_audit.compute_repo_label", return_value="ws/repo"):
            with pytest.raises(SystemExit):
                # Invoke Click command via standalone_mode=False to pass kwargs directly.
                run_pr_audit.main.main(
                    args=[], standalone_mode=False,
                    obj={},
                )

    def test_jira_team_flag_writes_jira_space(self, monkeypatch, tmp_path):
        self._run_main_with_message("--team PlatformTeam", monkeypatch, tmp_path)
        assert os.environ.get("JIRA_SPACE") == "PlatformTeam"

    def test_jira_boards_flag_writes_jira_boards(self, monkeypatch, tmp_path):
        extra = {"JIRA_BASE_URL": "https://jira.example.com", "JIRA_EMAIL": "u@e.com", "JIRA_API_TOKEN": "tok"}
        self._run_main_with_message("--board 42", monkeypatch, tmp_path, extra_env=extra)
        assert os.environ.get("JIRA_BOARDS") == "42"

    def test_pr_filter_flag_writes_pr_audit_filter(self, monkeypatch, tmp_path):
        self._run_main_with_message('--filter "Priority Area"=Backend', monkeypatch, tmp_path)
        assert os.environ.get("PR_AUDIT_FILTER") == "Priority Area=Backend"


class TestPromptTemplate:
    def test_template_renders(self):
        """Verify the prompt template can be formatted without KeyError."""
        result = _PR_AUDIT_PROMPT_TEMPLATE.format(
            repo_label="org/repo",
            branch="main",
            n_prs=50,
            date_from="2026-08-15",
            date_to="2026-09-15",
            jiras_reviewed=42,
            focus="all",
            pr_context="## PRs\n(test data)",
            skill_instructions="Analyze the PRs.",
            pr_ids="1,2,3",
        )
        assert "org/repo" in result
        assert "2026-08-15" in result
        assert "42" in result
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
