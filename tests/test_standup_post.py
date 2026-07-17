"""Tests for scripts/standup/post.py"""

import json
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

BRIEF = """\
📋 *Standup Brief — 2026-07-17*

• **Alice:** Working on PROJ-1.

🔴 PROJ-5 blocked 3d — needs decision.

1. Descope PROJ-5 or reassign?
"""

RISK_REPORT = "Sprint health: 4/10 done, 8 days left."

SYNTH_RESULT = {
    "status": "DONE",
    "risk_count": 1,
    "discussion_questions": 1,
    "silence_count": 0,
}


@patch("scripts.standup.post.post_message", return_value=True)
def test_main_writes_report_and_posts(mock_post, tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))

    (tmp_workspace / "standup_brief.md").write_text(BRIEF)
    (tmp_workspace / "risk_report.md").write_text(RISK_REPORT)
    (tmp_workspace / "synthesize_result.json").write_text(json.dumps(SYNTH_RESULT))

    from scripts.standup.post import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0

    report = (tmp_workspace / "standup_report.md").read_text()
    assert "Standup Report" in report
    assert "Alice" in report
    assert "Full Risk Report" in report

    result = json.loads((tmp_workspace / "post_result.json").read_text())
    assert result["status"] == "DONE"
    assert result["slack_posted"] is True
    assert result["risk_count"] == 1

    mock_post.assert_called_once()
    assert "Alice" in mock_post.call_args.args[1]


@patch("scripts.standup.post.post_message", return_value=False)
def test_main_still_succeeds_when_slack_fails(mock_post, tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))

    (tmp_workspace / "standup_brief.md").write_text(BRIEF)
    (tmp_workspace / "synthesize_result.json").write_text(json.dumps(SYNTH_RESULT))

    from scripts.standup.post import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0

    result = json.loads((tmp_workspace / "post_result.json").read_text())
    assert result["status"] == "DONE"
    assert result["slack_posted"] is False


def test_main_exits_1_when_no_brief(tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))

    from scripts.standup.post import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 1


@patch("scripts.standup.post.post_message", return_value=True)
def test_main_no_risk_report(mock_post, tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))

    (tmp_workspace / "standup_brief.md").write_text(BRIEF)
    # No risk_report.md
    (tmp_workspace / "synthesize_result.json").write_text(json.dumps(SYNTH_RESULT))

    from scripts.standup.post import main
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0

    report = (tmp_workspace / "standup_report.md").read_text()
    assert "Full Risk Report" not in report
