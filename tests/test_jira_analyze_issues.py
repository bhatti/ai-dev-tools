"""Tests for scripts/jira/analyze_issues.py"""
from unittest.mock import ANY, MagicMock, patch

from click.testing import CliRunner

from scripts.common.jira_api import extract_jira_keys
from scripts.jira.analyze_issues import _extract_text_from_doc, _format_for_analysis, main


def test_extract_keys_bare_keys():
    assert extract_jira_keys("PROJ-123,PROJ-456") == ["PROJ-123", "PROJ-456"]


def test_extract_keys_from_urls():
    assert extract_jira_keys(
        "https://company.atlassian.net/browse/PROJ-43911, PROJ-43909"
    ) == ["PROJ-43911", "PROJ-43909"]


def test_extract_keys_mixed():
    keys = extract_jira_keys(
        "https://company.atlassian.net/browse/PROJ-1,PROJ-2,https://x.atlassian.net/browse/PROJ-3"
    )
    # URL-sourced keys come first, then bare keys
    assert set(keys) == {"PROJ-1", "PROJ-2", "PROJ-3"}
    assert len(keys) == 3


def test_extract_keys_empty():
    assert extract_jira_keys("") == []
    assert extract_jira_keys("not-a-key, also-not") == []


def test_extract_text_from_doc_plain():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Hello world"}
        ]}
    ]}
    assert "Hello world" in _extract_text_from_doc(doc)


def test_extract_text_from_doc_none():
    assert _extract_text_from_doc(None) == ""


def test_format_for_analysis_includes_key():
    issues = [{
        "key": "PROJ-99",
        "fields": {
            "summary": "[Flaky Test] foo.ts",
            "status": {"name": "To Do"},
            "assignee": None,
            "priority": {"name": "High"},
            "description": "Test fails intermittently",
        },
    }]
    text = _format_for_analysis(issues, "https://company.atlassian.net")
    assert "PROJ-99" in text
    assert "Flaky Test" in text
    assert "company.atlassian.net/browse/PROJ-99" in text


@patch("scripts.jira.analyze_issues.find_skill", return_value=None)
@patch("scripts.jira.analyze_issues.find_skill_for_query", return_value=None)
@patch("scripts.jira.analyze_issues._try_git_archaeology", return_value=(None, None))
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.common.issue_analysis.run_claude")
@patch("scripts.jira.analyze_issues.notify")
def test_main_analyze_by_keys(mock_notify, mock_claude, mock_get_issue, mock_arch, mock_find, mock_find_skill):
    mock_get_issue.return_value = {
        "key": "PROJ-42",
        "fields": {"summary": "[Flaky] foo", "status": {"name": "To Do"},
                   "assignee": None, "priority": {"name": "High"}, "description": ""},
    }
    mock_claude.return_value = MagicMock(output="Root cause: race condition\nFix: add mutex", status="DONE")
    runner = CliRunner()
    env = {
        "JIRA_PROJECT": "PROJ", "JIRA_EMAIL": "u@e.com",
        "JIRA_API_TOKEN": "tok", "JIRA_BASE_URL": "https://company.atlassian.net",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--issues", "PROJ-42"], env=env)
    assert result.exit_code == 0
    mock_notify.assert_called_once()
    text = mock_notify.call_args[0][1]
    assert "PROJ-42" in text
    assert "race condition" in text


@patch("scripts.jira.query_issues._resolve_team_field_id", return_value=None)
@patch("scripts.common.jira_api.search_issues", return_value=[])
@patch("scripts.jira.analyze_issues.notify")
def test_main_no_results(mock_notify, mock_search, mock_resolve):
    runner = CliRunner()
    env = {
        "JIRA_PROJECT": "PROJ", "JIRA_EMAIL": "u@e.com",
        "JIRA_API_TOKEN": "tok", "JIRA_BASE_URL": "https://company.atlassian.net",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--query", "nonexistent"], env=env)
    assert result.exit_code == 2
    mock_notify.assert_called_once()


@patch("scripts.jira.analyze_issues.find_skill")
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.jira.analyze_issues.run_skill_analysis")
@patch("scripts.jira.analyze_issues.notify")
def test_relevant_skill_invoked_for_query(mock_notify, mock_skill_analysis, mock_get_issue, mock_find_skill):
    from pathlib import Path
    mock_get_issue.return_value = {
        "key": "PROJ-42",
        "fields": {"summary": "[Flaky] foo", "status": {"name": "To Do"},
                   "assignee": None, "priority": {"name": "High"}, "description": ""},
    }
    mock_find_skill.return_value = Path("/fake/SKILL.md")
    mock_skill_analysis.return_value = "Analysis: test is flaky due to race condition"
    runner = CliRunner()
    env = {
        "JIRA_PROJECT": "PROJ", "JIRA_EMAIL": "u@e.com",
        "JIRA_API_TOKEN": "tok", "JIRA_BASE_URL": "https://company.atlassian.net",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--issues", "PROJ-42"], env=env)
    assert result.exit_code == 0
    assert "SKILL_USED::ygs-analyze" in result.output


@patch("scripts.jira.analyze_issues.find_skill", return_value=None)
@patch("scripts.jira.analyze_issues.find_skill_for_query", return_value=None)
@patch("scripts.jira.analyze_issues._try_git_archaeology",
       return_value=("## Git History Context\n- abc fix", None))
@patch("scripts.common.issue_analysis.run_claude")
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.jira.analyze_issues.notify")
def test_no_skill_falls_back_to_git_archaeology(mock_notify, mock_get_issue, mock_claude, mock_arch, mock_find, mock_find_skill):
    mock_get_issue.return_value = {
        "key": "PROJ-99",
        "fields": {"summary": "Bug in auth", "status": {"name": "In Progress"},
                   "assignee": None, "priority": {"name": "Medium"}, "description": ""},
    }
    mock_claude.return_value = MagicMock(output="Root cause found", status="DONE")
    runner = CliRunner()
    env = {
        "JIRA_PROJECT": "PROJ", "JIRA_EMAIL": "u@e.com",
        "JIRA_API_TOKEN": "tok", "JIRA_BASE_URL": "https://company.atlassian.net",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--issues", "PROJ-99"], env=env)
    assert result.exit_code == 0
    assert "GIT_ARCHAEOLOGY::yes" in result.output


@patch("scripts.jira.analyze_issues.find_skill", return_value=None)
@patch("scripts.jira.analyze_issues.find_skill_for_query", return_value=None)
@patch("scripts.jira.analyze_issues._try_git_archaeology", return_value=(None, None))
@patch("scripts.common.issue_analysis.run_claude")
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.jira.analyze_issues.notify")
def test_no_git_archaeology_when_no_bb_config(mock_notify, mock_get_issue, mock_claude, mock_arch, mock_find, mock_find_skill):
    mock_get_issue.return_value = {
        "key": "PROJ-1",
        "fields": {"summary": "Bug", "status": {"name": "Open"},
                   "assignee": None, "priority": {"name": "Low"}, "description": ""},
    }
    mock_claude.return_value = MagicMock(output="analysis", status="DONE")
    runner = CliRunner()
    env = {
        "JIRA_PROJECT": "PROJ", "JIRA_EMAIL": "u@e.com",
        "JIRA_API_TOKEN": "tok", "JIRA_BASE_URL": "https://company.atlassian.net",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--issues", "PROJ-1"], env=env)
    assert result.exit_code == 0
    assert "GIT_ARCHAEOLOGY::no" in result.output


@patch("scripts.jira.analyze_issues.find_skill")
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.jira.analyze_issues.run_skill_analysis")
@patch("scripts.jira.analyze_issues.notify")
def test_direct_ygs_analyze_skill_lookup(mock_notify, mock_skill_analysis, mock_get_issue, mock_find_skill):
    """find_skill("ygs-analyze") called directly — no keyword matching needed."""
    from pathlib import Path
    mock_get_issue.return_value = {
        "key": "PROJ-123",
        "fields": {"summary": "Some bug", "status": {"name": "Open"},
                   "assignee": None, "priority": {"name": "High"}, "description": ""},
    }
    mock_find_skill.return_value = Path("/skills/ygs-analyze/SKILL.md")
    mock_skill_analysis.return_value = "Deep analysis result"
    runner = CliRunner()
    env = {
        "JIRA_PROJECT": "PROJ", "JIRA_EMAIL": "u@e.com",
        "JIRA_API_TOKEN": "tok", "JIRA_BASE_URL": "https://company.atlassian.net",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--issues", "PROJ-123"], env=env)
    assert result.exit_code == 0
    mock_find_skill.assert_called_with("ygs-analyze", ANY)
    assert "SKILL_USED::ygs-analyze" in result.output
    assert "ANALYSIS_TYPE::skill" in result.output


@patch("scripts.jira.analyze_issues.find_skill", return_value=None)
@patch("scripts.jira.analyze_issues.find_skill_for_query")
@patch("scripts.jira.analyze_issues._try_git_archaeology", return_value=(None, None))
@patch("scripts.common.issue_analysis.run_claude")
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.jira.analyze_issues.notify")
def test_prompt_option_used_for_skill_fallback(
    mock_notify, mock_get_issue, mock_claude, mock_arch, mock_find_query, mock_find_skill
):
    """--prompt text is passed to find_skill_for_query when direct lookup fails."""
    mock_get_issue.return_value = {
        "key": "PROJ-7",
        "fields": {"summary": "slowdown", "status": {"name": "Open"},
                   "assignee": None, "priority": {"name": "Low"}, "description": ""},
    }
    mock_find_query.return_value = None
    mock_claude.return_value = MagicMock(output="analysis", status="DONE")
    runner = CliRunner()
    env = {
        "JIRA_PROJECT": "PROJ", "JIRA_EMAIL": "u@e.com",
        "JIRA_API_TOKEN": "tok", "JIRA_BASE_URL": "https://company.atlassian.net",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--issues", "PROJ-7", "--prompt", "give tldr for PROJ-7"], env=env)
    assert result.exit_code == 0
    args, _ = mock_find_query.call_args
    assert "give tldr for PROJ-7" in args[0]


@patch("scripts.jira.analyze_issues.find_skill", return_value=None)
@patch("scripts.jira.analyze_issues.find_skill_for_query", return_value=None)
@patch("scripts.jira.analyze_issues._try_git_archaeology", return_value=(None, None))
@patch("scripts.common.issue_analysis.run_claude")
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.jira.analyze_issues.notify")
def test_analysis_type_basic_when_no_skill_no_git(
    mock_notify, mock_get_issue, mock_claude, mock_arch, mock_find_query, mock_find_skill
):
    """When no skill and no git context, ANALYSIS_TYPE::basic is emitted."""
    mock_get_issue.return_value = {
        "key": "PROJ-3",
        "fields": {"summary": "basic bug", "status": {"name": "Open"},
                   "assignee": None, "priority": {"name": "Low"}, "description": ""},
    }
    mock_claude.return_value = MagicMock(output="basic analysis", status="DONE")
    runner = CliRunner()
    env = {
        "JIRA_PROJECT": "PROJ", "JIRA_EMAIL": "u@e.com",
        "JIRA_API_TOKEN": "tok", "JIRA_BASE_URL": "https://company.atlassian.net",
        "WORKSPACE_DIR": "/tmp",
    }
    result = runner.invoke(main, ["--issues", "PROJ-3"], env=env)
    assert result.exit_code == 0
    assert "ANALYSIS_TYPE::basic" in result.output
    assert "GIT_ARCHAEOLOGY::no" in result.output


@patch("scripts.jira.analyze_issues.find_skill", return_value=None)
@patch("scripts.jira.analyze_issues.find_skill_for_query", return_value=None)
@patch("scripts.jira.analyze_issues._try_git_archaeology", return_value=(None, None))
@patch("scripts.common.issue_analysis.run_claude")
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.jira.analyze_issues.notify")
def test_richer_task_context_emitted(
    mock_notify, mock_get_issue, mock_claude, mock_arch, mock_find_query, mock_find_skill
):
    """Verify that tracker, model, and issue count context keys are emitted."""
    mock_get_issue.return_value = {
        "key": "PROJ-5",
        "fields": {"summary": "context test", "status": {"name": "Open"},
                   "assignee": None, "priority": {"name": "Medium"}, "description": ""},
    }
    mock_claude.return_value = MagicMock(output="result", status="DONE")
    runner = CliRunner()
    env = {
        "JIRA_PROJECT": "PROJ", "JIRA_EMAIL": "u@e.com",
        "JIRA_API_TOKEN": "tok", "JIRA_BASE_URL": "https://company.atlassian.net",
        "WORKSPACE_DIR": "/tmp", "AI_MODEL": "claude-sonnet-test",
    }
    result = runner.invoke(main, ["--issues", "PROJ-5"], env=env)
    assert result.exit_code == 0
    assert "SELECTED_TRACKER::jira" in result.output
    assert "SELECTED_MODEL::claude-sonnet-test" in result.output
    assert "ISSUE_COUNT::1" in result.output
