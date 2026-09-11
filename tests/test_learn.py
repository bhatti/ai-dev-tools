"""Tests for learn.py posting helpers and prompt construction."""

import subprocess
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# jira/learn.py helpers
# ---------------------------------------------------------------------------

class TestPostJiraComment:
    def test_posts_when_all_creds_present(self):
        from scripts.jira.learn import _post_jira_comment
        with patch("scripts.jira.learn._requests.post") as mock_post:
            mock_post.return_value = MagicMock(status_code=201)
            config = {
                "JIRA_BASE_URL": "https://jira.example.com",
                "JIRA_EMAIL": "user@example.com",
                "JIRA_API_TOKEN": "token123",
            }
            _post_jira_comment(config, "PROJ-42", "# Post-Merge Analysis\nsome content")
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            assert "PROJ-42/comment" in call_kwargs[0][0]
            assert call_kwargs[1]["json"]["body"].startswith("# Post-Merge")

    def test_skips_when_missing_url(self):
        from scripts.jira.learn import _post_jira_comment
        with patch("scripts.jira.learn._requests.post") as mock_post:
            config = {"JIRA_EMAIL": "u@x.com", "JIRA_API_TOKEN": "t"}
            _post_jira_comment(config, "PROJ-1", "body")
            mock_post.assert_not_called()

    def test_skips_when_missing_issue_key(self):
        from scripts.jira.learn import _post_jira_comment
        with patch("scripts.jira.learn._requests.post") as mock_post:
            config = {
                "JIRA_BASE_URL": "https://jira.example.com",
                "JIRA_EMAIL": "u@x.com",
                "JIRA_API_TOKEN": "t",
            }
            _post_jira_comment(config, "", "body")
            mock_post.assert_not_called()

    def test_nonfatal_on_exception(self):
        from scripts.jira.learn import _post_jira_comment
        with patch("scripts.jira.learn._requests.post", side_effect=Exception("network error")):
            config = {
                "JIRA_BASE_URL": "https://jira.example.com",
                "JIRA_EMAIL": "u@x.com",
                "JIRA_API_TOKEN": "t",
            }
            # Must not raise
            _post_jira_comment(config, "PROJ-1", "body")

    def test_truncates_long_body(self):
        from scripts.jira.learn import _post_jira_comment
        with patch("scripts.jira.learn._requests.post") as mock_post:
            mock_post.return_value = MagicMock()
            config = {
                "JIRA_BASE_URL": "https://jira.example.com",
                "JIRA_EMAIL": "u@x.com",
                "JIRA_API_TOKEN": "t",
            }
            long_body = "x" * 40000
            _post_jira_comment(config, "PROJ-1", long_body)
            posted_body = mock_post.call_args[1]["json"]["body"]
            assert len(posted_body) <= 30000


class TestPostBbComment:
    def test_posts_when_all_args_present(self):
        from scripts.jira.learn import _post_bb_comment
        with patch("scripts.jira.learn.add_pr_comment") as mock_add:
            config = {"BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"}
            _post_bb_comment(config, "cribl", "cribl", 45974, "report text")
            mock_add.assert_called_once_with(config, "cribl", "cribl", 45974, "report text")

    def test_skips_when_workspace_missing(self):
        from scripts.jira.learn import _post_bb_comment
        with patch("scripts.jira.learn.add_pr_comment") as mock_add:
            _post_bb_comment({}, "", "repo", 1, "body")
            mock_add.assert_not_called()

    def test_skips_when_pr_id_missing(self):
        from scripts.jira.learn import _post_bb_comment
        with patch("scripts.jira.learn.add_pr_comment") as mock_add:
            _post_bb_comment({}, "ws", "repo", None, "body")
            mock_add.assert_not_called()

    def test_nonfatal_on_exception(self):
        from scripts.jira.learn import _post_bb_comment
        with patch("scripts.jira.learn.add_pr_comment", side_effect=Exception("api error")):
            # Must not raise
            _post_bb_comment({}, "ws", "repo", 1, "body")

    def test_truncates_long_report(self):
        from scripts.jira.learn import _post_bb_comment
        with patch("scripts.jira.learn.add_pr_comment") as mock_add:
            long_report = "y" * 20000
            _post_bb_comment({}, "ws", "repo", 1, long_report)
            posted = mock_add.call_args[0][4]
            assert len(posted) <= 8000


# ---------------------------------------------------------------------------
# gh/learn.py helpers
# ---------------------------------------------------------------------------

class TestPostGhComment:
    def test_posts_to_pr(self):
        from scripts.gh.learn import _post_gh_comment
        with patch("scripts.gh.learn.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _post_gh_comment("pr", 9, "bhatti", "todo-sample", "# Analysis\nbody")
            cmd = mock_run.call_args[0][0]
            assert "pr" in cmd
            assert "comment" in cmd
            assert "9" in cmd
            assert "bhatti/todo-sample" in " ".join(cmd)

    def test_posts_to_issue(self):
        from scripts.gh.learn import _post_gh_comment
        with patch("scripts.gh.learn.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _post_gh_comment("issue", "1", "bhatti", "todo-sample", "body")
            cmd = mock_run.call_args[0][0]
            assert "issue" in cmd
            assert "1" in cmd

    def test_skips_when_org_missing(self):
        from scripts.gh.learn import _post_gh_comment
        with patch("scripts.gh.learn.subprocess.run") as mock_run:
            _post_gh_comment("pr", 9, "", "todo-sample", "body")
            mock_run.assert_not_called()

    def test_nonfatal_on_nonzero_returncode(self):
        from scripts.gh.learn import _post_gh_comment
        with patch("scripts.gh.learn.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="not found")
            # Must not raise
            _post_gh_comment("pr", 9, "org", "repo", "body")

    def test_nonfatal_on_exception(self):
        from scripts.gh.learn import _post_gh_comment
        with patch("scripts.gh.learn.subprocess.run", side_effect=Exception("timeout")):
            # Must not raise
            _post_gh_comment("pr", 9, "org", "repo", "body")

    def test_truncates_long_body(self):
        from scripts.gh.learn import _post_gh_comment
        with patch("scripts.gh.learn.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            long_body = "z" * 20000
            _post_gh_comment("pr", 9, "org", "repo", long_body)
            body_arg = mock_run.call_args[0][0][-1]  # last arg is --body value
            assert len(body_arg) <= 8000


# ---------------------------------------------------------------------------
# LEARN_PROMPT_TEMPLATE — format variable coverage
# ---------------------------------------------------------------------------

class TestLearnPromptTemplate:
    def test_jira_template_has_required_placeholders(self):
        from scripts.jira.learn import LEARN_PROMPT_TEMPLATE
        for placeholder in ["{issue_id}", "{title}", "{pr_context}", "{impl_summary}", "{comments_text}"]:
            assert placeholder in LEARN_PROMPT_TEMPLATE, f"Missing: {placeholder}"

    def test_jira_template_has_phase0_instructions(self):
        from scripts.jira.learn import LEARN_PROMPT_TEMPLATE
        assert "Phase 0" in LEARN_PROMPT_TEMPLATE
        assert "PR Health Check" in LEARN_PROMPT_TEMPLATE
        assert "has_acceptance_criteria" in LEARN_PROMPT_TEMPLATE
        assert "rubber_stamp_approvers" in LEARN_PROMPT_TEMPLATE

    def test_jira_template_has_ygs_learn_reference(self):
        from scripts.jira.learn import LEARN_PROMPT_TEMPLATE
        assert "/ygs-learn" in LEARN_PROMPT_TEMPLATE

    def test_jira_template_output_format_updated(self):
        from scripts.jira.learn import LEARN_PROMPT_TEMPLATE
        assert "health_signals" in LEARN_PROMPT_TEMPLATE

    def test_gh_template_matches_jira_template_structure(self):
        from scripts.jira.learn import LEARN_PROMPT_TEMPLATE as jira_tmpl
        from scripts.gh.learn import LEARN_PROMPT_TEMPLATE as gh_tmpl
        for key in ["Phase 0", "PR Health Check", "/ygs-learn", "health_signals"]:
            assert key in gh_tmpl, f"GH template missing: {key}"
            assert key in jira_tmpl, f"Jira template missing: {key}"

    def test_templates_share_body_from_common_module(self):
        from scripts.common.learn_prompts import JIRA_LEARN_PROMPT_TEMPLATE, GH_LEARN_PROMPT_TEMPLATE
        from scripts.jira.learn import LEARN_PROMPT_TEMPLATE as jira_alias
        from scripts.gh.learn import LEARN_PROMPT_TEMPLATE as gh_alias
        assert jira_alias is JIRA_LEARN_PROMPT_TEMPLATE
        assert gh_alias is GH_LEARN_PROMPT_TEMPLATE

    def test_jira_header_uses_issue_id_without_hash(self):
        from scripts.common.learn_prompts import JIRA_LEARN_PROMPT_TEMPLATE
        # Jira uses "PROJ-42: Title" not "Issue #42"
        result = JIRA_LEARN_PROMPT_TEMPLATE.format(
            issue_id="PROJ-42", title="Fix bug",
            pr_context="", impl_summary="", comments_text="",
        )
        assert "## PROJ-42: Fix bug" in result
        assert "## Issue #" not in result

    def test_gh_header_uses_issue_hash_prefix(self):
        from scripts.common.learn_prompts import GH_LEARN_PROMPT_TEMPLATE
        result = GH_LEARN_PROMPT_TEMPLATE.format(
            issue_id="9", title="Add feature",
            pr_context="", impl_summary="", comments_text="",
        )
        assert "## Issue #9: Add feature" in result


# ---------------------------------------------------------------------------
# Shared health check prompts
# ---------------------------------------------------------------------------

class TestHealthCheckPromptsShared:
    def test_health_check_dims_appear_in_learn_prompt_body(self):
        from scripts.common.learn_prompts import _LEARN_PROMPT_BODY
        assert "Spec Coverage" in _LEARN_PROMPT_BODY
        assert "CI Health" in _LEARN_PROMPT_BODY
        assert "Review Quality" in _LEARN_PROMPT_BODY

    def test_health_check_dims_has_all_five_dimensions(self):
        from scripts.common.health_check_prompts import HEALTH_CHECK_DIMS
        assert "1." in HEALTH_CHECK_DIMS
        assert "5." in HEALTH_CHECK_DIMS
        assert "Spec Coverage" in HEALTH_CHECK_DIMS
        assert "Design Decisions" in HEALTH_CHECK_DIMS
        assert "Security & SRE" in HEALTH_CHECK_DIMS
        assert "Review Quality" in HEALTH_CHECK_DIMS
        assert "CI Health" in HEALTH_CHECK_DIMS

    def test_health_check_dims_no_format_placeholders(self):
        import re
        from scripts.common.health_check_prompts import HEALTH_CHECK_DIMS
        placeholders = re.findall(r'\{([^}]*)\}', HEALTH_CHECK_DIMS)
        assert placeholders == [], f"HEALTH_CHECK_DIMS has format placeholders: {placeholders}"

    def test_learn_prompts_import_from_health_check_module(self):
        from scripts.common.health_check_prompts import HEALTH_CHECK_DIMS
        from scripts.common.learn_prompts import _LEARN_PROMPT_BODY
        # All dimension keywords from the shared constant must appear in the learn body
        for keyword in ["Spec Coverage", "Design Decisions", "Security & SRE", "Review Quality", "CI Health"]:
            assert keyword in _LEARN_PROMPT_BODY, f"Missing '{keyword}' in _LEARN_PROMPT_BODY"
