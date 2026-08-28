"""Tests for scripts/standup/synthesize.py"""

import json
from unittest.mock import MagicMock, patch

import pytest

from scripts.standup.synthesize import _build_prompt


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
# _build_prompt
# ---------------------------------------------------------------------------

def test_build_prompt_includes_signals():
    signals = _make_signals()
    signals["team_members"] = ["Alice", "Bob"]
    prompt = _build_prompt(signals)
    # Prompt tells Claude to invoke /ygs-standup and read signals.json directly.
    assert "ygs-standup" in prompt
    assert "Alice" in prompt  # team member appears in TEAM ROSTER
    assert "signals.json" in prompt


def test_build_prompt_trims_long_comments():
    # Prompt no longer embeds signals JSON, so comment trimming is no longer done here.
    # This test just verifies _build_prompt runs without error on signals with long comments.
    signals = _make_signals()
    long_text = "x" * 500
    signals["issues"][0]["recent_comments"] = [
        {"author": "Bob", "text": long_text, "created": "2026-07-17T09:00:00Z"}
    ]
    prompt = _build_prompt(signals)
    assert "ygs-standup" in prompt


def test_build_prompt_keeps_max_3_comments():
    # Prompt no longer embeds signals JSON — just verify _build_prompt runs without error.
    signals = _make_signals()
    signals["issues"][0]["recent_comments"] = [
        {"author": f"User{i}", "text": f"comment {i}", "created": "2026-07-17T09:00:00Z"}
        for i in range(6)
    ]
    prompt = _build_prompt(signals)
    assert "ygs-standup" in prompt


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

    # Simulate Claude writing the output files (as the skill instructs it to)
    def _fake_run_claude(*args, **kwargs):
        (tmp_workspace / "standup_brief.md").write_text(
            "📋 *Standup Brief*\n• **Alice:** Working on PROJ-1.\n"
        )
        (tmp_workspace / "risk_report.md").write_text(
            "Sprint 3: 4/10 done, 8 days left.\nNo HIGH risks.\n"
        )
        return MagicMock(
            exit_code=0,
            output=SAMPLE_OUTPUT,
            status_json={"status": "DONE", "risk_count": 0, "discussion_questions": 1, "silence_count": 0},
            status="DONE",
        )

    mock_claude.side_effect = _fake_run_claude

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
