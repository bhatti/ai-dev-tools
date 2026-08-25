"""Tests for scripts/common/issue_analysis.py (shared analysis logic)."""
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch


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
