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
        assert result["team_members"] is None
        assert result["jira_boards"] is None
        assert result["gh_milestone"] is None

    def test_combined_team_and_milestone(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit last 20 prs --team alice --milestone sprint-3"})
        assert result["n_prs"] == 20
        assert result["team_members"] == "alice"
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
    """--board with a GitHub-tracker job should warn and NOT set PR_AUDIT_JIRA_BOARDS."""

    def _run_main_stub(self, config_overrides: dict, env_overrides: dict) -> tuple[dict, list[str]]:
        """Run just the board-flag processing branch of main() in isolation."""
        import io as _io
        from unittest.mock import patch as _patch
        import scripts.analyze.run_pr_audit as m

        captured = []

        def fake_print(*args, **kwargs):
            captured.append(" ".join(str(a) for a in args))

        config = {"DEFAULT_TRACKER": "github", **config_overrides}
        env = {**env_overrides}

        with _patch("builtins.print", side_effect=fake_print), \
             _patch.dict(os.environ, env, clear=False):
            flags = m._parse_slack_flags({"SLACK_MESSAGE": "pr-audit --board 123"})
            board_val = flags["jira_boards"]
            if board_val == "__default__":
                board_val = config.get("JIRA_BOARDS", "") or ""
            if board_val:
                tracker = config.get("DEFAULT_TRACKER", "").lower()
                if tracker == "github":
                    fake_print(
                        "[pr-audit] WARNING: --board scopes by Jira/Bitbucket active-sprint assignees "
                        "and has no effect for GitHub-tracker jobs; use --milestone to scope by milestone"
                    )
                else:
                    os.environ["PR_AUDIT_JIRA_BOARDS"] = board_val
                    config["PR_AUDIT_JIRA_BOARDS"] = board_val

        return config, captured

    def test_board_with_gh_tracker_warns_and_does_not_set_env(self):
        config, output = self._run_main_stub({"DEFAULT_TRACKER": "github"}, {})
        assert "PR_AUDIT_JIRA_BOARDS" not in config
        assert any("WARNING" in line and "--milestone" in line for line in output)

    def test_board_with_jira_tracker_sets_env(self):
        import scripts.analyze.run_pr_audit as m
        flags = m._parse_slack_flags({"SLACK_MESSAGE": "pr-audit --board 456"})
        assert flags["jira_boards"] == "456"


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
