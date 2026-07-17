"""Tests for scripts/standup/synthesize.py"""

import json
from unittest.mock import MagicMock, patch

import pytest

from scripts.standup.synthesize import _build_prompt, _clean_code_fence, _extract_section


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_signals(tracker="jira", issue_count=2, pr_count=1):
    issues = []
    for i in range(issue_count):
        issues.append({
            "key": f"PROJ-{i+1}",
            "summary": f"Issue {i+1}",
            "status": "In Progress",
            "assignee": "Alice" if i == 0 else "Bob",
            "updated": "2026-07-16T10:00:00+00:00",
            "stale_days": 0,
            "is_stale": False,
            "is_blocked": False,
            "labels": [],
            "priority": "Medium",
            "recent_comments": [],
        })
    prs = []
    for i in range(pr_count):
        prs.append({
            "id": i + 1,
            "title": f"PR {i+1}",
            "author": "Alice",
            "age_hours": 5.0,
            "reviewers": ["Bob"],
            "url": "https://example.com/pr/1",
        })
    return {
        "gathered_at": "2026-07-17T09:00:00+00:00",
        "tracker": tracker,
        "sprint": {"name": "Sprint 3", "end_date": "2026-07-25"},
        "issues": issues,
        "open_prs": prs,
        "slack_messages": [],
        "config_summary": {
            "jira_project": "PROJ",
            "lookback_hours": 26,
            "stale_days": 2,
        },
    }


# ---------------------------------------------------------------------------
# _extract_section
# ---------------------------------------------------------------------------

def test_extract_section_found():
    text = """
#### STANDUP_BRIEF
This is the brief content.

#### RISK_REPORT
This is the risk report.
"""
    assert "brief content" in _extract_section(text, "STANDUP_BRIEF")
    assert "risk report" in _extract_section(text, "RISK_REPORT")


def test_extract_section_not_found():
    assert _extract_section("no headings here", "STANDUP_BRIEF") == ""


def test_extract_section_last():
    text = "#### RISK_REPORT\nRisk content only"
    assert "Risk content only" in _extract_section(text, "RISK_REPORT")


# ---------------------------------------------------------------------------
# _clean_code_fence
# ---------------------------------------------------------------------------

def test_clean_code_fence_strips_markers():
    text = "```\n📋 Brief content\n```"
    assert _clean_code_fence(text) == "📋 Brief content"


def test_clean_code_fence_no_fences():
    text = "Plain text"
    assert _clean_code_fence(text) == "Plain text"


# ---------------------------------------------------------------------------
# _build_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_includes_signals():
    signals = _make_signals()
    prompt = _build_prompt(signals)
    assert "PROJ-1" in prompt
    assert "Alice" in prompt
    assert "ygs-standup" in prompt
    assert "ygs-risk-scan" in prompt


def test_build_prompt_trims_long_comments():
    signals = _make_signals()
    long_text = "x" * 500
    signals["issues"][0]["recent_comments"] = [
        {"author": "Bob", "text": long_text, "created": "2026-07-17T09:00:00Z"}
    ]
    prompt = _build_prompt(signals)
    # After trimming, no full 500-char run should appear in the prompt
    assert "x" * 400 not in prompt


def test_build_prompt_keeps_max_3_comments():
    signals = _make_signals()
    signals["issues"][0]["recent_comments"] = [
        {"author": f"User{i}", "text": f"comment {i}", "created": "2026-07-17T09:00:00Z"}
        for i in range(6)
    ]
    prompt = _build_prompt(signals)
    # Should not include all 6 comments verbatim (trimmed to last 3)
    assert "comment 0" not in prompt  # oldest dropped


# ---------------------------------------------------------------------------
# main — mocked claude
# ---------------------------------------------------------------------------

SAMPLE_OUTPUT = """\
#### STANDUP_BRIEF
📋 *Standup Brief — 2026-07-17*

*Per-person status*
• **Alice:** Working on PROJ-1.

*Risks*
ℹ️ No HIGH risks today.

*Discussion (bring to the meeting)*
1. Review sprint velocity.

#### RISK_REPORT
Sprint 3: 4/10 done, 8 days left.
No HIGH risks.

{"status":"DONE","risk_count":0,"discussion_questions":1,"silence_count":0}
"""


@patch("scripts.standup.synthesize.validate_claude_config")
@patch("scripts.standup.synthesize.run_claude")
def test_main_writes_artifacts(mock_claude, mock_validate, tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))

    signals = _make_signals()
    (tmp_workspace / "signals.json").write_text(json.dumps(signals))

    mock_claude.return_value = MagicMock(
        exit_code=0,
        output=SAMPLE_OUTPUT,
        status_json={"status": "DONE", "risk_count": 0, "discussion_questions": 1, "silence_count": 0},
        status="DONE",
    )

    from scripts.standup.synthesize import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0

    brief = (tmp_workspace / "standup_brief.md").read_text()
    assert "Alice" in brief

    risk = (tmp_workspace / "risk_report.md").read_text()
    assert "Sprint 3" in risk

    result = json.loads((tmp_workspace / "synthesize_result.json").read_text())
    assert result["status"] == "DONE"


@patch("scripts.standup.synthesize.validate_claude_config")
@patch("scripts.standup.synthesize.run_claude")
def test_main_missing_signals(mock_claude, mock_validate, tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))

    from scripts.standup.synthesize import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1
    mock_claude.assert_not_called()
