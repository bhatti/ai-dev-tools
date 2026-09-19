"""Tests for scripts/common/issue_analysis.py (shared analysis logic)."""
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_write_analysis_output_creates_artifacts(tmp_path):
    from scripts.common.issue_analysis import write_analysis_output

    config = {"WORKSPACE_DIR": str(tmp_path)}
    write_analysis_output(config, ["PROJ-1", "PROJ-2"], "Root cause: race condition")

    reports = tmp_path / "reports"
    result = json.loads((reports / "result.json").read_text())
    assert result["count"] == 2
    assert result["keys"] == ["PROJ-1", "PROJ-2"]
    assert "race condition" in result["analysis"]

    md = (reports / "report.md").read_text()
    assert "PROJ-1" in md
    assert "race condition" in md

    html = (reports / "report.html").read_text()
    assert "<html" in html.lower()


def test_write_analysis_output_empty_ids(tmp_path):
    from scripts.common.issue_analysis import write_analysis_output

    config = {"WORKSPACE_DIR": str(tmp_path)}
    write_analysis_output(config, [], "No issues found.")

    result = json.loads((tmp_path / "reports" / "result.json").read_text())
    assert result["count"] == 0
    assert result["keys"] == []


def test_write_analysis_output_skill_path_renders_html_from_report_md(tmp_path):
    """When write_html=False and skill already wrote report.md, render report.html from it."""
    from scripts.common.issue_analysis import write_analysis_output

    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    (reports / "report.md").write_text("## Root Cause\n\nRace condition in lock.", encoding="utf-8")

    config = {"WORKSPACE_DIR": str(tmp_path)}
    write_analysis_output(config, ["PROJ-3"], "ignored", write_html=False)

    assert (reports / "result.json").exists()
    html = (reports / "report.html").read_text()
    assert "<html" in html.lower()
    assert "Root Cause" in html


def test_write_analysis_output_skill_path_no_report_md(tmp_path):
    """When write_html=False and no report.md, only result.json is written."""
    from scripts.common.issue_analysis import write_analysis_output

    config = {"WORKSPACE_DIR": str(tmp_path)}
    write_analysis_output(config, ["PROJ-4"], "ignored", write_html=False)

    assert (tmp_path / "reports" / "result.json").exists()
    assert not (tmp_path / "reports" / "report.html").exists()


@pytest.mark.parametrize("signoff", [
    "Full report at reports/report.md.",          # with period
    "Full report at reports/report.md",           # without period
    "FULL REPORT AT REPORTS/REPORT.MD.",          # uppercase
    "Full report at reports/report.md.  ",        # trailing spaces
    "\n\nFull report at reports/report.md.",      # extra newlines before
])
@patch("scripts.common.issue_analysis.run_claude")
def test_run_skill_analysis_strips_trailing_signoff(mock_run_claude, tmp_path, signoff):
    """Trailing 'Full report at reports/report.md' variants are stripped from skill output."""
    from scripts.common.issue_analysis import run_skill_analysis

    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    (reports / "report.md").write_text(
        f"## Analysis\n\nRace condition.\n{signoff}",
        encoding="utf-8",
    )
    skill_md = tmp_path / "SKILL.md"
    skill_md.write_text("# Skill", encoding="utf-8")
    mock_run_claude.return_value = MagicMock(output='{"status":"DONE"}', status="DONE")
    config = {"WORKSPACE_DIR": str(tmp_path)}

    result = run_skill_analysis(config, "issue text", "ygs-analyze", skill_md)

    assert "Full report" not in result
    assert "Race condition" in result


@patch("scripts.common.issue_analysis.run_claude")
def test_run_analysis_returns_output(mock_run_claude, tmp_path):
    from scripts.common.issue_analysis import run_analysis

    mock_run_claude.return_value = MagicMock(output="  Analysis result here  ", status="DONE")
    config = {"WORKSPACE_DIR": str(tmp_path)}
    result = run_analysis(config, "Issue: flaky test")

    assert result == "Analysis result here"
    mock_run_claude.assert_called_once()
    call_kwargs = mock_run_claude.call_args
    assert "flaky test" in call_kwargs[0][0]


@patch("scripts.common.issue_analysis.run_claude")
def test_run_analysis_uses_custom_prompt_template(mock_run_claude, tmp_path):
    from scripts.common.issue_analysis import run_analysis

    mock_run_claude.return_value = MagicMock(output="custom result", status="DONE")
    config = {
        "WORKSPACE_DIR": str(tmp_path),
        "ANALYSIS_PROMPT": "Custom template: {issues_text}",
    }
    run_analysis(config, "issue text here")

    prompt_used = mock_run_claude.call_args[0][0]
    assert "Custom template" in prompt_used
    assert "issue text here" in prompt_used
