"""Tests for scripts/analyze/run_pr_audit.py"""

import os
from unittest.mock import patch

import pytest

from scripts.analyze.run_pr_audit import (
    _filter_prs_by_state,
    _parse_slack_flags,
    _PR_AUDIT_PROMPT_TEMPLATE,
    _resolve_effective_tracker,
    _resolve_branch,
)
from scripts.analyze.pr_fetcher import size_bucket, compute_pr_state_summary


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
             patch("scripts.analyze.run_pr_audit.ensure_ygs_skills"), \
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
            pr_state_summary="merged=48  open=1  declined=1  total=50",
            metrics_summary="### Category Breakdown\n| Category | PRs |",
        )
        assert "org/repo" in result
        assert "2026-08-15" in result
        assert "42" in result
        assert "50" in result
        assert "all" in result
        assert "Analyze the PRs." in result
        assert "Category Breakdown" in result
        assert "pr_audit_report.md" in result
        assert "pr_audit_findings.json" in result
        assert "skill_improvements.json" in result

    def test_template_has_required_outputs(self):
        """Verify the template mentions all three required output files."""
        assert "pr_audit_report.md" in _PR_AUDIT_PROMPT_TEMPLATE
        assert "pr_audit_findings.json" in _PR_AUDIT_PROMPT_TEMPLATE
        assert "skill_improvements.json" in _PR_AUDIT_PROMPT_TEMPLATE

    def test_template_has_state_gate(self):
        """Verify the state-gate section is present so it can't be accidentally deleted."""
        assert "state=declined" in _PR_AUDIT_PROMPT_TEMPLATE
        assert "MANDATORY" in _PR_AUDIT_PROMPT_TEMPLATE

    def test_template_has_state_summary_placeholder(self):
        """Verify the pr_state_summary placeholder is wired into the template."""
        assert "{pr_state_summary}" in _PR_AUDIT_PROMPT_TEMPLATE

    def test_template_has_metrics_summary_placeholder(self):
        """Verify the metrics_summary placeholder is wired into the template."""
        assert "{metrics_summary}" in _PR_AUDIT_PROMPT_TEMPLATE

    def test_metrics_summary_includes_per_pr_table(self):
        """Verify _build_metrics_summary produces complete metrics section."""
        from scripts.analyze.run_pr_audit import _build_metrics_summary
        prs = [
            {"pr_number": 1, "author": "alice", "category": "api",
             "pr_type": "bug", "blast_radius": "low", "risk_score": 25.0,
             "risk_tier": "medium", "total_loc": 150, "file_count": 5,
             "complexity": "medium", "is_hotspot": True,
             "url": "https://github.com/org/repo/pull/1",
             "title": "Fix API validation",
             "linked_issue": {"key": "PROJ-1", "url": "https://jira.example.com/browse/PROJ-1"}},
        ]
        summary = _build_metrics_summary(prs)
        assert "Per-PR Metrics" in summary
        assert "[#1]" in summary
        assert "🔥" in summary
        assert "Cat" in summary
        assert "Issue" in summary
        assert "Title" in summary
        assert "[PROJ-1]" in summary
        assert "Pre-Computed PR Metrics Summary" in summary


class TestPRSizeBucket:
    def test_xs_boundary(self):
        assert size_bucket(0, 0) == "xs"
        assert size_bucket(25, 24) == "xs"    # 49 LOC
        assert size_bucket(25, 25) == "s"     # 50 LOC

    def test_s_boundary(self):
        assert size_bucket(100, 99) == "s"    # 199 LOC
        assert size_bucket(100, 100) == "m"   # 200 LOC

    def test_m_boundary(self):
        assert size_bucket(250, 249) == "m"   # 499 LOC
        assert size_bucket(250, 250) == "l"   # 500 LOC

    def test_l_boundary(self):
        assert size_bucket(500, 499) == "l"   # 999 LOC
        assert size_bucket(500, 500) == "xl"  # 1000 LOC

    def test_xl_large(self):
        assert size_bucket(5000, 4999) == "xl"


class TestComputePRStateSummary:
    def test_all_merged(self):
        prs = [{"state": "merged"}] * 3
        result = compute_pr_state_summary(prs)
        assert result == {"merged": 3, "open": 0, "declined": 0, "total": 3}

    def test_mixed_states(self):
        prs = [
            {"state": "merged"},
            {"state": "merged"},
            {"state": "declined"},
            {"state": "open"},
        ]
        result = compute_pr_state_summary(prs)
        assert result == {"merged": 2, "open": 1, "declined": 1, "total": 4}

    def test_legacy_closed_counts_as_declined(self):
        """Ensure old 'closed' state (pre-fix) still increments declined not lost."""
        prs = [{"state": "closed"}]
        result = compute_pr_state_summary(prs)
        assert result["declined"] == 1

    def test_empty(self):
        result = compute_pr_state_summary([])
        assert result == {"merged": 0, "open": 0, "declined": 0, "total": 0}


class TestAppendMetricsSections:
    """Verify pre-computed section is always appended, even when Claude writes its own dashboard."""

    def _make_prs(self, n=5):
        return [
            {"pr_number": i, "author": "alice", "category": "api", "pr_type": "feature",
             "blast_radius": "low", "risk_score": 10.0, "risk_tier": "low",
             "total_loc": 50, "file_count": 2, "complexity": "low", "is_hotspot": False,
             "url": f"https://github.com/org/repo/pull/{i}", "title": f"PR {i}"}
            for i in range(1, n + 1)
        ]

    def test_pre_computed_appended_when_claude_writes_dashboard(self, tmp_path):
        from scripts.analyze.run_pr_audit import _build_metrics_summary_with_claude
        # Simulate Claude already writing a "Metrics Dashboard" — pre-computed section must still appear
        md = "# PR Audit\n\n## Metrics Dashboard\n\n| Metric | Value |\n|--------|-------|\n| foo | bar |\n"
        result = md
        prs = self._make_prs()
        claude_dashboard_written = "Metrics Dashboard" in result
        if "Pre-Computed PR Metrics" not in result:
            pre_rows = [] if claude_dashboard_written else []
            appended = _build_metrics_summary_with_claude(prs, pre_rows)
            result += f"\n\n{appended}\n"
        assert "Pre-Computed PR Metrics Summary" in result
        assert "Metrics Dashboard" in result  # Claude's original still present

    def test_pre_computed_appended_when_no_dashboard(self, tmp_path):
        from scripts.analyze.run_pr_audit import _build_metrics_summary_with_claude
        md = "# PR Audit\n\nSome findings.\n"
        prs = self._make_prs()
        appended = _build_metrics_summary_with_claude(prs, [])
        result = md + f"\n\n{appended}\n"
        assert "Pre-Computed PR Metrics Summary" in result

    def test_build_metrics_summary_with_claude_includes_all_subsections(self):
        from scripts.analyze.run_pr_audit import _build_metrics_summary_with_claude
        prs = self._make_prs(5)
        result = _build_metrics_summary_with_claude(prs, [])
        assert "Pre-Computed PR Metrics Summary" in result
        assert "Per-PR Metrics" in result or "PR #" in result or "alice" in result
        assert "Metrics Dashboard" in result


# ---------------------------------------------------------------------------
# --state flag parsing
# ---------------------------------------------------------------------------

class TestParseSlackFlagsState:
    """--state flag is parsed from Slack message and stored as pr_state."""

    def test_state_merged_only(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --state merged"})
        assert result["pr_state"] == "merged"

    def test_state_declined_only(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --state declined"})
        assert result["pr_state"] == "declined"

    def test_state_open(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --state open"})
        assert result["pr_state"] == "open"

    def test_state_all(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit --state all"})
        assert result["pr_state"] == "all"

    def test_state_absent_returns_none(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "pr-audit last 20 prs"})
        assert result.get("pr_state") is None

    def test_empty_message_pr_state_absent_or_none(self):
        result = _parse_slack_flags({})
        assert result.get("pr_state") is None


# ---------------------------------------------------------------------------
# PR state post-filter — _filter_prs_by_state
# ---------------------------------------------------------------------------

_MIXED_PRS = [
    {"id": "1", "state": "merged",   "title": "Merged"},
    {"id": "2", "state": "open",     "title": "Still open"},
    {"id": "3", "state": "declined", "title": "Declined"},
]


class TestFilterPrsByState:
    """Tests for _filter_prs_by_state — the real production function."""

    def test_default_excludes_open(self):
        result = _filter_prs_by_state(_MIXED_PRS, ["merged", "declined"])
        assert {p["id"] for p in result} == {"1", "3"}

    def test_state_all_passthrough(self):
        result = _filter_prs_by_state(_MIXED_PRS, ["merged", "declined", "open"])
        assert result is _MIXED_PRS  # pass-through, not a copy

    def test_state_all_order_independent(self):
        """Set semantics: any ordering of the three states must pass through."""
        result = _filter_prs_by_state(_MIXED_PRS, ["open", "merged", "declined"])
        assert result is _MIXED_PRS

    def test_state_open_only(self):
        result = _filter_prs_by_state(_MIXED_PRS, ["open"])
        assert [p["id"] for p in result] == ["2"]

    def test_state_merged_only(self):
        result = _filter_prs_by_state(_MIXED_PRS, ["merged"])
        assert [p["id"] for p in result] == ["1"]

    def test_missing_state_defaults_to_merged(self):
        """PRs without a state field default to 'merged' and survive merged+declined filter."""
        prs = [{"id": "9", "title": "no state field"}]
        result = _filter_prs_by_state(prs, ["merged", "declined"])
        assert len(result) == 1

    def test_open_pr_url_dropped_by_default(self):
        prs = [
            {"id": "10", "state": "merged", "title": "Shipped"},
            {"id": "11", "state": "open",   "title": "Draft"},
        ]
        result = _filter_prs_by_state(prs, ["merged", "declined"])
        assert [p["id"] for p in result] == ["10"]

    def test_open_pr_url_kept_with_state_all(self):
        prs = [
            {"id": "10", "state": "merged", "title": "Shipped"},
            {"id": "11", "state": "open",   "title": "Draft"},
        ]
        result = _filter_prs_by_state(prs, ["merged", "declined", "open"])
        assert len(result) == 2

    def test_empty_prs_returns_empty(self):
        assert _filter_prs_by_state([], ["merged", "declined"]) == []


class TestDesignMetricsRows:
    """New design-principle metric rows appear in _CLAUDE_METRIC_ROWS and produce correct signals."""

    def test_design_metric_keys_in_claude_rows(self):
        from scripts.analyze.run_pr_audit import _CLAUDE_METRIC_ROWS
        assert "solid_violation_rate" in _CLAUDE_METRIC_ROWS
        assert "hotspot_coupling_count" in _CLAUDE_METRIC_ROWS
        assert "avg_readability_score" in _CLAUDE_METRIC_ROWS

    def test_design_metric_keys_in_context_keys(self):
        from scripts.analyze.run_pr_audit import _METRIC_CONTEXT_KEYS
        assert "solid_violation_rate" in _METRIC_CONTEXT_KEYS
        assert "hotspot_coupling_count" in _METRIC_CONTEXT_KEYS
        assert "avg_readability_score" in _METRIC_CONTEXT_KEYS

    def test_context_keys_and_metric_rows_are_in_sync(self):
        """The assert in the module guards this, but make it explicit in tests too."""
        from scripts.analyze.run_pr_audit import _CLAUDE_METRIC_ROWS, _METRIC_CONTEXT_KEYS
        assert set(_METRIC_CONTEXT_KEYS) == set(_CLAUDE_METRIC_ROWS)

    def test_solid_violation_rate_signal_green(self):
        from scripts.analyze.run_pr_audit import _metric_signal
        assert _metric_signal("solid_violation_rate", 0.3) == "🟢"

    def test_solid_violation_rate_signal_yellow(self):
        from scripts.analyze.run_pr_audit import _metric_signal
        assert _metric_signal("solid_violation_rate", 1.0) == "🟡"

    def test_solid_violation_rate_signal_red(self):
        from scripts.analyze.run_pr_audit import _metric_signal
        assert _metric_signal("solid_violation_rate", 2.0) == "🔴"

    def test_hotspot_coupling_count_signal_green(self):
        from scripts.analyze.run_pr_audit import _metric_signal
        assert _metric_signal("hotspot_coupling_count", 0) == "🟢"

    def test_hotspot_coupling_count_signal_yellow(self):
        from scripts.analyze.run_pr_audit import _metric_signal
        assert _metric_signal("hotspot_coupling_count", 2) == "🟡"

    def test_hotspot_coupling_count_signal_red(self):
        from scripts.analyze.run_pr_audit import _metric_signal
        assert _metric_signal("hotspot_coupling_count", 4) == "🔴"

    def test_avg_readability_score_signal_green(self):
        from scripts.analyze.run_pr_audit import _metric_signal
        assert _metric_signal("avg_readability_score", 8.0) == "🟢"

    def test_avg_readability_score_signal_yellow(self):
        from scripts.analyze.run_pr_audit import _metric_signal
        assert _metric_signal("avg_readability_score", 6.0) == "🟡"

    def test_avg_readability_score_signal_red(self):
        from scripts.analyze.run_pr_audit import _metric_signal
        assert _metric_signal("avg_readability_score", 3.0) == "🔴"

    def test_read_all_claude_metrics_returns_design_rows(self, tmp_path):
        """_read_all_claude_metrics parses design metric keys from pr_audit_findings.json."""
        import json
        from scripts.analyze.run_pr_audit import _read_all_claude_metrics
        findings = {
            "metrics": {
                "solid_violation_rate": 0.8,
                "hotspot_coupling_count": 3,
                "avg_readability_score": 6.5,
            }
        }
        (tmp_path / "pr_audit_findings.json").write_text(json.dumps(findings))
        rows = _read_all_claude_metrics(tmp_path)
        names = [r[0] for r in rows]
        assert "SOLID Violation Rate" in names
        assert "Hotspot Coupling" in names
        assert "Avg Readability Score" in names

    def test_design_rows_appear_in_dashboard(self, tmp_path):
        """Design metric extra_rows flow through build_metrics_dashboard correctly."""
        import json
        from scripts.analyze.run_pr_audit import _read_all_claude_metrics
        from scripts.common.pr_classify import build_metrics_dashboard
        findings = {"metrics": {"solid_violation_rate": 1.2, "hotspot_coupling_count": 2, "avg_readability_score": 5.5}}
        (tmp_path / "pr_audit_findings.json").write_text(json.dumps(findings))
        rows = _read_all_claude_metrics(tmp_path)
        dashboard = build_metrics_dashboard([], extra_rows=rows)
        assert "SOLID Violation Rate" in dashboard
        assert "Hotspot Coupling" in dashboard
        assert "Avg Readability Score" in dashboard
