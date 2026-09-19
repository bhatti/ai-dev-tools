"""Tests for scripts/jira/analyze_issues.py"""
from unittest.mock import ANY, MagicMock, patch

from click.testing import CliRunner

from scripts.common.jira_api import extract_adf_text, extract_jira_keys
from scripts.common.issue_analysis import format_jira_issues_for_analysis as _format_for_analysis
from scripts.jira.analyze_issues import main


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
    assert set(keys) == {"PROJ-1", "PROJ-2", "PROJ-3"}
    assert len(keys) == 3


def test_extract_keys_empty():
    assert extract_jira_keys("") == []
    assert extract_jira_keys("not-a-key, also-not") == []


def test_extract_adf_text_plain():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [
            {"type": "text", "text": "Hello world"}
        ]}
    ]}
    assert "Hello world" in extract_adf_text(doc)


def test_extract_adf_text_none():
    assert extract_adf_text(None) == ""


def test_extract_adf_text_string():
    assert extract_adf_text("plain text") == "plain text"


def test_format_for_analysis_includes_key():
    issues = [{
        "key": "PROJ-99",
        "fields": {
            "summary": "[Flaky Test] foo.ts",
            "status": {"name": "To Do"},
            "assignee": None,
            "priority": {"name": "High"},
            "description": "Test fails intermittently",
            "issuelinks": [],
            "attachment": [],
        },
    }]
    text = _format_for_analysis(issues, "https://company.atlassian.net", {}, {})
    assert "PROJ-99" in text
    assert "Flaky Test" in text
    assert "company.atlassian.net/browse/PROJ-99" in text


def test_format_for_analysis_includes_linked_issues():
    issues = [{
        "key": "PROJ-100",
        "id": "100",
        "fields": {
            "summary": "Bug with links",
            "status": {"name": "Open"},
            "assignee": None,
            "priority": {"name": "High"},
            "description": "",
            "attachment": [],
            "issuelinks": [{
                "type": {"name": "Blocks"},
                "inwardIssue": {
                    "key": "PROJ-200",
                    "fields": {"summary": "Blocked issue"},
                },
            }],
        },
    }]
    text = _format_for_analysis(issues, "https://company.atlassian.net", {}, {})
    assert "PROJ-200" in text
    assert "Blocks" in text


def test_format_for_analysis_includes_linked_prs():
    issues = [{
        "key": "PROJ-101",
        "id": "101",
        "fields": {
            "summary": "Bug with PR",
            "status": {"name": "Open"},
            "assignee": None,
            "priority": {"name": "Low"},
            "description": "",
            "attachment": [],
            "issuelinks": [],
        },
    }]
    prs_map = {"PROJ-101": [{"id": "42", "name": "Fix bug", "url": "https://bb/pr/42", "status": "MERGED"}]}
    text = _format_for_analysis(issues, "https://company.atlassian.net", prs_map, {})
    assert "#42" in text
    assert "Fix bug" in text


_BASE_ENV = {
    "JIRA_PROJECT": "PROJ", "JIRA_EMAIL": "u@e.com",
    "JIRA_API_TOKEN": "tok", "JIRA_BASE_URL": "https://company.atlassian.net",
    "WORKSPACE_DIR": "/tmp",
}

_MOCK_ISSUE = {
    "key": "PROJ-42",
    "id": "42",
    "fields": {
        "summary": "[Flaky] foo",
        "status": {"name": "To Do"},
        "assignee": None,
        "priority": {"name": "High"},
        "description": "",
        "issuelinks": [],
        "attachment": [],
    },
}


@patch("scripts.jira.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.jira.analyze_issues.resolve_skill_for_analyze", return_value=None)
@patch("scripts.jira.analyze_issues.fetch_jira_issue_full", return_value=None)
@patch("scripts.jira.analyze_issues.get_jira_linked_prs", return_value=[])
@patch("scripts.jira.analyze_issues.fetch_jira_attachment_text", return_value=None)
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.jira.analyze_issues.run_analysis")
@patch("scripts.jira.analyze_issues.post_report")
def test_main_analyze_by_keys(mock_post, mock_claude, mock_get_issue,
                               mock_attach, mock_prs, mock_fetch_full,
                               mock_skill, mock_arch):
    mock_get_issue.return_value = _MOCK_ISSUE
    mock_fetch_full.return_value = None  # fallback to raw
    mock_claude.return_value = "Root cause: race condition\nFix: add mutex"
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "PROJ-42"], env=_BASE_ENV)
    assert result.exit_code == 0
    mock_post.assert_called_once()
    text = mock_post.call_args[0][1]
    assert "PROJ-42" in text
    assert "race condition" in text


@patch("scripts.jira.query_issues.resolve_field_id", return_value=None)
@patch("scripts.common.jira_api.search_issues", return_value=[])
@patch("scripts.jira.analyze_issues.post_report")
def test_main_no_results(mock_post, mock_search, mock_resolve):
    runner = CliRunner()
    result = runner.invoke(main, ["--query", "nonexistent"], env=_BASE_ENV)
    assert result.exit_code == 2
    mock_post.assert_called_once()


@patch("scripts.jira.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.jira.analyze_issues.resolve_skill_for_analyze")
@patch("scripts.jira.analyze_issues.fetch_jira_issue_full", return_value=None)
@patch("scripts.jira.analyze_issues.get_jira_linked_prs", return_value=[])
@patch("scripts.jira.analyze_issues.fetch_jira_attachment_text", return_value=None)
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.jira.analyze_issues.run_skill_analysis")
@patch("scripts.jira.analyze_issues.post_report")
def test_relevant_skill_invoked_for_query(mock_post, mock_skill_analysis, mock_get_issue,
                                           mock_attach, mock_prs, mock_fetch_full,
                                           mock_resolve_skill, mock_arch):
    from pathlib import Path
    mock_get_issue.return_value = _MOCK_ISSUE
    mock_resolve_skill.return_value = ("ygs-analyze", Path("/fake/SKILL.md"))
    mock_skill_analysis.return_value = "Analysis: test is flaky due to race condition"
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "PROJ-42"], env=_BASE_ENV)
    assert result.exit_code == 0
    assert "SKILL_USED::ygs-analyze" in result.output


@patch("scripts.jira.analyze_issues.try_git_archaeology",
       return_value=("## Git History Context\n- abc fix", None))
@patch("scripts.jira.analyze_issues.resolve_skill_for_analyze", return_value=None)
@patch("scripts.jira.analyze_issues.emit_git_context_markers", return_value="📂 *Git context:* cloned\n")
@patch("scripts.jira.analyze_issues.fetch_jira_issue_full", return_value=None)
@patch("scripts.jira.analyze_issues.get_jira_linked_prs", return_value=[])
@patch("scripts.jira.analyze_issues.fetch_jira_attachment_text", return_value=None)
@patch("scripts.jira.analyze_issues.run_analysis")
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.jira.analyze_issues.post_report")
def test_no_skill_falls_back_to_git_archaeology(mock_post, mock_get_issue, mock_claude,
                                                 mock_attach, mock_prs, mock_fetch_full,
                                                 mock_markers, mock_skill, mock_arch):
    mock_get_issue.return_value = _MOCK_ISSUE
    mock_claude.return_value = "Root cause found"
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "PROJ-99"], env=_BASE_ENV)
    assert result.exit_code == 0
    assert "GIT_ARCHAEOLOGY::yes" in result.output


@patch("scripts.jira.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.jira.analyze_issues.resolve_skill_for_analyze", return_value=None)
@patch("scripts.jira.analyze_issues.fetch_jira_issue_full", return_value=None)
@patch("scripts.jira.analyze_issues.get_jira_linked_prs", return_value=[])
@patch("scripts.jira.analyze_issues.fetch_jira_attachment_text", return_value=None)
@patch("scripts.jira.analyze_issues.run_analysis")
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.jira.analyze_issues.post_report")
def test_no_git_archaeology_when_no_bb_config(mock_post, mock_get_issue, mock_claude,
                                               mock_attach, mock_prs, mock_fetch_full,
                                               mock_skill, mock_arch):
    mock_get_issue.return_value = _MOCK_ISSUE
    mock_claude.return_value = "analysis"
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "PROJ-1"], env=_BASE_ENV)
    assert result.exit_code == 0
    assert "GIT_ARCHAEOLOGY::no" in result.output


@patch("scripts.jira.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.jira.analyze_issues.resolve_skill_for_analyze")
@patch("scripts.jira.analyze_issues.fetch_jira_issue_full", return_value=None)
@patch("scripts.jira.analyze_issues.get_jira_linked_prs", return_value=[])
@patch("scripts.jira.analyze_issues.fetch_jira_attachment_text", return_value=None)
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.jira.analyze_issues.run_skill_analysis")
@patch("scripts.jira.analyze_issues.post_report")
def test_direct_ygs_analyze_skill_lookup(mock_post, mock_skill_analysis, mock_get_issue,
                                          mock_attach, mock_prs, mock_fetch_full,
                                          mock_resolve_skill, mock_arch):
    """resolve_skill_for_analyze returns ygs-analyze — SKILL_USED::ygs-analyze emitted."""
    from pathlib import Path
    mock_get_issue.return_value = _MOCK_ISSUE
    mock_resolve_skill.return_value = ("ygs-analyze", Path("/skills/ygs-analyze/SKILL.md"))
    mock_skill_analysis.return_value = "Deep analysis result"
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "PROJ-123"], env=_BASE_ENV)
    assert result.exit_code == 0
    assert "SKILL_USED::ygs-analyze" in result.output
    assert "ANALYSIS_TYPE::skill" in result.output


@patch("scripts.jira.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.jira.analyze_issues.resolve_skill_for_analyze", return_value=None)
@patch("scripts.jira.analyze_issues.fetch_jira_issue_full", return_value=None)
@patch("scripts.jira.analyze_issues.get_jira_linked_prs", return_value=[])
@patch("scripts.jira.analyze_issues.fetch_jira_attachment_text", return_value=None)
@patch("scripts.jira.analyze_issues.run_analysis")
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.jira.analyze_issues.post_report")
def test_richer_task_context_emitted(mock_post, mock_get_issue, mock_claude,
                                      mock_attach, mock_prs, mock_fetch_full,
                                      mock_skill, mock_arch):
    """Verify tracker, model, issue count, and new enrichment context keys are emitted."""
    mock_get_issue.return_value = _MOCK_ISSUE
    mock_claude.return_value = "result"
    env = dict(_BASE_ENV, AI_MODEL="claude-sonnet-test")
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "PROJ-5"], env=env)
    assert result.exit_code == 0
    assert "SELECTED_TRACKER::jira" in result.output
    assert "SELECTED_MODEL::claude-sonnet-test" in result.output
    assert "ISSUE_COUNT::1" in result.output
    assert "ISSUE_LINKS_COUNT::0" in result.output
    assert "PR_LINKS_COUNT::0" in result.output
    assert "ATTACHMENTS_COUNT::0" in result.output


@patch("scripts.jira.analyze_issues.try_git_archaeology", return_value=(None, None))
@patch("scripts.jira.analyze_issues.resolve_skill_for_analyze", return_value=None)
@patch("scripts.jira.analyze_issues.fetch_jira_issue_full", return_value=None)
@patch("scripts.jira.analyze_issues.get_jira_linked_prs", return_value=[])
@patch("scripts.jira.analyze_issues.fetch_jira_attachment_text", return_value=None)
@patch("scripts.jira.analyze_issues.run_analysis")
@patch("scripts.common.jira_api.get_issue")
@patch("scripts.jira.analyze_issues.post_report")
def test_analysis_type_basic_when_no_skill_no_git(mock_post, mock_get_issue, mock_claude,
                                                    mock_attach, mock_prs, mock_fetch_full,
                                                    mock_skill, mock_arch):
    """When no skill and no git context, ANALYSIS_TYPE::basic is emitted."""
    mock_get_issue.return_value = _MOCK_ISSUE
    mock_claude.return_value = "basic analysis"
    runner = CliRunner()
    result = runner.invoke(main, ["--issues", "PROJ-3"], env=_BASE_ENV)
    assert result.exit_code == 0
    assert "ANALYSIS_TYPE::basic" in result.output
    assert "GIT_ARCHAEOLOGY::no" in result.output
