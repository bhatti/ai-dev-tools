"""The ai-skill job's reporting contract: what run_skill keeps and verifies, and what the post step sends to Slack."""

from __future__ import annotations

import json

import pytest

from scripts.common.claude_runner import ClaudeResult
from scripts.skill import post, run_skill


class TestToolBackedSkillsMustRunTheirTools:
    def _workspace(self, tmp_path, result=None):
        (tmp_path / "reports").mkdir()
        if result is not None:
            (tmp_path / "reports" / "result.json").write_text(json.dumps(result))
        return tmp_path

    @pytest.mark.parametrize("skill", ["ygs-sprint-ready", "ygs-enrich-ticket"])
    def test_a_run_without_the_tools_result_is_a_failure_not_a_green_job(self, tmp_path, skill):
        assert "did not run its tooling" in run_skill.missing_tool_output(skill, self._workspace(tmp_path))

    @pytest.mark.parametrize("skill", ["ygs-sprint-ready", "ygs-enrich-ticket"])
    def test_a_result_the_model_wrote_by_hand_does_not_count(self, tmp_path, skill):
        assert run_skill.missing_tool_output(skill, self._workspace(tmp_path, {"status": "DONE", "summary": "all good"}))

    def test_a_real_readiness_result_passes(self, tmp_path):
        assert run_skill.missing_tool_output("ygs-sprint-ready", self._workspace(tmp_path, {"tickets": [], "summary": {"total": 0}})) is None

    def test_a_corrupt_result_is_reported_not_raised(self, tmp_path):
        ws = self._workspace(tmp_path)
        (ws / "reports" / "result.json").write_text("{not json")
        assert run_skill.missing_tool_output("ygs-sprint-ready", ws)

    def test_a_result_left_over_from_before_the_run_does_not_count(self, tmp_path):
        ws = self._workspace(tmp_path, {"tickets": [], "summary": {}})
        started = (ws / "reports" / "result.json").stat().st_mtime + 10
        assert "before this run" in run_skill.missing_tool_output("ygs-sprint-ready", ws, since=started)
        assert run_skill.missing_tool_output("ygs-sprint-ready", ws, since=started - 20) is None

    def test_other_skills_are_not_held_to_this_contract(self, tmp_path):
        assert run_skill.missing_tool_output("ygs-ask", self._workspace(tmp_path)) is None


class TestReportsWrittenByATool:
    def test_the_tools_result_json_is_kept_when_the_job_writes_its_reports(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "result.json").write_text(json.dumps({"tickets": [{"id": "PROJ-1"}], "summary": {"total": 1}}))
        run_skill._write_reports(tmp_path, "ygs-sprint-ready", "# Ticket readiness\n", {"status": "DONE", "signal": "DONE"}, model="m")
        assert json.loads((reports / "result.json").read_text())["tickets"] == [{"id": "PROJ-1"}]
        assert (reports / "report.md").exists() and (reports / "report.html").exists()

    def test_without_one_the_status_line_is_written_as_before(self, tmp_path):
        run_skill._write_reports(tmp_path, "ygs-ask", "answer", {"status": "DONE"}, model="m")
        assert json.loads((tmp_path / "reports" / "result.json").read_text()) == {"status": "DONE"}

    def test_other_skills_behave_exactly_as_before_even_when_a_result_exists(self, tmp_path):
        (tmp_path / "reports").mkdir()
        (tmp_path / "reports" / "result.json").write_text(json.dumps({"stale": True}))
        run_skill._write_reports(tmp_path, "ygs-ask", "answer", {"status": "DONE"}, model="m")
        assert json.loads((tmp_path / "reports" / "result.json").read_text()) == {"status": "DONE"}


class TestSlackPostPrefersTheToolsSummary:
    def _run(self, tmp_path, monkeypatch, slack_md=None):
        (tmp_path / "reports").mkdir()
        (tmp_path / "reports" / "report.md").write_text("# Full report\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n")
        if slack_md is not None:
            (tmp_path / "reports" / "slack.md").write_text(slack_md)
        (tmp_path / "skill_result.json").write_text(json.dumps({"skill": "ygs-sprint-ready", "status": "DONE"}))
        posted = []
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        monkeypatch.setattr(post, "post_report", lambda config, slack_text, md_text, **kw: posted.append(slack_text) or True)
        with pytest.raises(SystemExit) as e:
            post.main()
        assert e.value.code == 0
        return posted

    def test_slack_md_is_posted_as_it_is(self, tmp_path, monkeypatch):
        assert self._run(tmp_path, monkeypatch, "*Ticket readiness* summary\n• PROJ-1\n") == ["*Ticket readiness* summary\n• PROJ-1"]

    def test_without_it_the_report_is_converted_as_before(self, tmp_path, monkeypatch):
        text = self._run(tmp_path, monkeypatch)[0]
        assert "Full report" in text and "| --- |" not in text

    def test_secrets_in_a_summary_the_model_wrote_are_redacted_and_size_is_capped(self, tmp_path, monkeypatch):
        secret = "ghp_" + "a1B2c3D4e5" * 4
        text = self._run(tmp_path, monkeypatch, f"*Summary* {secret}\n" + "x" * 50000)[0]
        assert secret not in text and len(text) <= post.SLACK_MAX_CHARS

    def test_an_unreadable_summary_falls_back_to_the_report_instead_of_crashing(self, tmp_path, monkeypatch):
        (tmp_path / "reports").mkdir()
        (tmp_path / "reports" / "report.md").write_text("# Full report\n")
        (tmp_path / "reports" / "slack.md").write_bytes(b"\xff\xfe\x00bad")
        (tmp_path / "skill_result.json").write_text(json.dumps({"skill": "s", "status": "DONE"}))
        posted = []
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        monkeypatch.setattr(post, "post_report", lambda config, slack_text, md_text, **kw: posted.append(slack_text) or True)
        with pytest.raises(SystemExit) as e:
            post.main()
        assert e.value.code == 0 and "Full report" in posted[0]

    def test_a_summary_without_a_report_is_still_posted(self, tmp_path, monkeypatch):
        (tmp_path / "reports").mkdir()
        (tmp_path / "reports" / "slack.md").write_text("*Only a summary*\n")
        (tmp_path / "skill_result.json").write_text(json.dumps({"skill": "s", "status": "DONE"}))
        posted = []
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        monkeypatch.setattr(post, "post_report", lambda config, slack_text, md_text, **kw: posted.append(slack_text) or True)
        with pytest.raises(SystemExit):
            post.main()
        assert posted == ["*Only a summary*"]

    def test_an_empty_slack_md_falls_back_to_the_report(self, tmp_path, monkeypatch):
        assert "Full report" in self._run(tmp_path, monkeypatch, "  \n")[0]


class TestJobWiring:
    """run_skill.main end to end with the model stubbed: what the model leaves behind decides whether the job passes."""

    def _main(self, tmp_path, monkeypatch, *, tool_result):
        monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("RAW_ARGS", "ygs-sprint-ready --sprint X --dry-run")
        for name in ("DEFAULT_TRACKER", "CODEBASE_REPO_URL", "GH_ORG", "GH_REPO", "BITBUCKET_WORKSPACE", "BITBUCKET_REPO"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(run_skill, "_setup_environment", lambda config: None)
        monkeypatch.setattr(run_skill, "ensure_ygs_skills", lambda: None)
        monkeypatch.setattr(run_skill, "_load_skill_md", lambda skill: "# skill")

        def fake_claude(prompt, working_dir, **kw):
            if tool_result:
                (working_dir / "reports").mkdir(exist_ok=True)
                (working_dir / "reports" / "result.json").write_text(json.dumps({"tickets": [{"id": "PROJ-1"}], "summary": {"total": 1}}))
                (working_dir / "reports" / "report.md").write_text("# Ticket readiness\n")
            return ClaudeResult(exit_code=0, output="All done", status_json={"status": "DONE", "signal": "DONE"}, status="DONE")
        monkeypatch.setattr(run_skill, "run_claude", fake_claude)
        with pytest.raises(SystemExit) as e:
            run_skill.main()
        return e.value.code

    def test_a_run_that_used_its_tooling_passes_and_keeps_the_tools_result(self, tmp_path, monkeypatch):
        assert self._main(tmp_path, monkeypatch, tool_result=True) == 0
        assert json.loads((tmp_path / "reports" / "result.json").read_text())["tickets"] == [{"id": "PROJ-1"}]

    def test_a_run_that_only_talked_fails_the_job_even_though_the_model_said_done(self, tmp_path, monkeypatch, capsys):
        assert self._main(tmp_path, monkeypatch, tool_result=False) == 1
        assert "did not run its tooling" in capsys.readouterr().err
