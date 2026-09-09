"""Tests for scripts/analyze/plan_skill_updates.py"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path):
    return {
        "WORKSPACE_DIR": str(tmp_path),
        "AI_MODEL": "test-model",
        "MAX_TURNS_PLAN": "5",
        "GH_ORG": "test-org",
        "GH_REPO": "test-repo",
        "CLAUDE_CODE_USE_BEDROCK": "0",
    }


def _fake_run_claude_ok(prompt, working_dir, model, max_turns, log_file, system_prompt):
    """Simulates a successful Claude run that writes skill_update_plan.md."""
    (working_dir / "reports" / "skill_update_plan.md").write_text(
        "# Skill Update Plan\n\nPriority 1: update ygs-review.md",
        encoding="utf-8",
    )
    result = MagicMock()
    result.status = "DONE"
    result.status_json = {"status": "DONE", "skill_updates": 2, "new_skills": 1, "summary": "ok"}
    return result


def _fake_run_claude_error(prompt, working_dir, model, max_turns, log_file, system_prompt):
    raise RuntimeError("Claude subprocess failed")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPlanSkillUpdatesMissingAuditReport:
    """Exit 1 if pr_audit_report.md is not present."""

    @patch("scripts.analyze.plan_skill_updates.load_config")
    @patch("scripts.analyze.plan_skill_updates.validate_claude_config")
    def test_exits_1_when_audit_report_missing(self, mock_validate, mock_config, tmp_path):
        mock_config.return_value = _make_config(tmp_path)
        (tmp_path / "reports").mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        from scripts.analyze.plan_skill_updates import main

        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1


class TestPlanSkillUpdatesIdempotency:
    """Skip if skill_update_plan_result.json already shows status=DONE."""

    @patch("scripts.analyze.plan_skill_updates.load_config")
    @patch("scripts.analyze.plan_skill_updates.validate_claude_config")
    @patch("scripts.analyze.plan_skill_updates.run_claude")
    def test_skips_when_already_done(self, mock_claude, mock_validate, mock_config, tmp_path):
        mock_config.return_value = _make_config(tmp_path)
        reports = tmp_path / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        # Write pre-existing DONE result
        (reports / "skill_update_plan_result.json").write_text(
            json.dumps({"status": "DONE"}), encoding="utf-8"
        )
        (reports / "pr_audit_report.md").write_text("# Audit\n\nSome findings.", encoding="utf-8")

        from scripts.analyze.plan_skill_updates import main

        # check_done exits 0 when already DONE — that's the expected idempotency path
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
        mock_claude.assert_not_called()  # Claude should NOT be invoked again


class TestPlanSkillUpdatesSuccess:
    """Happy-path: audit report present, Claude writes plan, result JSON written."""

    @patch("scripts.analyze.plan_skill_updates.load_config")
    @patch("scripts.analyze.plan_skill_updates.validate_claude_config")
    @patch("scripts.analyze.plan_skill_updates.run_claude", side_effect=_fake_run_claude_ok)
    def test_writes_plan_and_result(self, mock_claude, mock_validate, mock_config, tmp_path):
        mock_config.return_value = _make_config(tmp_path)
        reports = tmp_path / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        (reports / "pr_audit_report.md").write_text(
            "# Audit\n\nTeam skips security review.", encoding="utf-8"
        )

        from scripts.analyze.plan_skill_updates import main

        main()

        assert (reports / "skill_update_plan.md").exists()
        result = json.loads((reports / "skill_update_plan_result.json").read_text())
        assert result["status"] == "DONE"
        assert result["skill_updates"] == 2

    @patch("scripts.analyze.plan_skill_updates.load_config")
    @patch("scripts.analyze.plan_skill_updates.validate_claude_config")
    @patch("scripts.analyze.plan_skill_updates.run_claude", side_effect=_fake_run_claude_ok)
    def test_reads_skill_improvements_if_present(self, mock_claude, mock_validate, mock_config, tmp_path):
        mock_config.return_value = _make_config(tmp_path)
        reports = tmp_path / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        (reports / "pr_audit_report.md").write_text("# Audit\n\nFindings.", encoding="utf-8")
        improvements = {"repo_skill_changes": [{"file_path": ".claude/skills/x.md"}], "new_docs": []}
        (reports / "skill_improvements.json").write_text(json.dumps(improvements), encoding="utf-8")

        from scripts.analyze.plan_skill_updates import main

        main()

        # The prompt passed to Claude should reference the improvements
        call_kwargs = mock_claude.call_args
        prompt_arg = call_kwargs[0][0]
        assert "repo_skill_changes" in prompt_arg or "skill_improvements" in prompt_arg.lower()


class TestPlanSkillUpdatesClaudeError:
    """Exit 1 if Claude subprocess raises RuntimeError."""

    @patch("scripts.analyze.plan_skill_updates.load_config")
    @patch("scripts.analyze.plan_skill_updates.validate_claude_config")
    @patch("scripts.analyze.plan_skill_updates.run_claude", side_effect=_fake_run_claude_error)
    def test_exits_1_on_claude_failure(self, mock_claude, mock_validate, mock_config, tmp_path):
        mock_config.return_value = _make_config(tmp_path)
        reports = tmp_path / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        (reports / "pr_audit_report.md").write_text("# Audit\n\nFindings.", encoding="utf-8")

        from scripts.analyze.plan_skill_updates import main

        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 1

        result = json.loads((reports / "skill_update_plan_result.json").read_text())
        assert result["status"] == "ERROR"
