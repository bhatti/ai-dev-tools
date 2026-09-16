"""Tests for scripts/standup/render_html.py"""

import json
import pytest
from datetime import date, timedelta
from unittest.mock import patch


def _make_signals(sprints=None, issues=None):
    today = date.today()
    end = (today + timedelta(days=3)).isoformat() + "T00:00:00Z"
    return {
        "gathered_at": today.isoformat(),
        "tracker": "jira",
        "current_user": {"displayName": "Test User"},
        "sprint": {"id": 1, "name": "Sprint 1", "board": "Board A", "end_date": end},
        "all_sprints": sprints or [
            {"id": 1, "name": "Sprint 1", "board": "Board A", "end_date": end},
        ],
        "issues": issues or [],
        "open_prs": [],
        "slack_messages": [],
        "config_summary": {"jira_project": "PROJ", "lookback_hours": 26},
    }


def _run(tmp_workspace, monkeypatch, signals, risk_md=""):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    (tmp_workspace / "signals.json").write_text(json.dumps(signals))
    if risk_md:
        (tmp_workspace / "risk_report.md").write_text(risk_md)

    import importlib
    import scripts.standup.render_html as mod
    importlib.reload(mod)

    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 0
    return (tmp_workspace / "reports" / "report.html").read_text()


def test_render_creates_html(tmp_workspace, monkeypatch):
    html = _run(tmp_workspace, monkeypatch, _make_signals())
    assert "<!doctype html" in html.lower()
    assert "Standup Report" in html


def test_render_shows_board_name(tmp_workspace, monkeypatch):
    html = _run(tmp_workspace, monkeypatch, _make_signals())
    assert "Board A" in html
    assert "Sprint 1" in html


def test_render_shared_sprint_shows_both_boards(tmp_workspace, monkeypatch):
    today = date.today()
    end = (today + timedelta(days=3)).isoformat() + "T00:00:00Z"
    signals = _make_signals(sprints=[
        {"id": 42, "name": "Chupacabra 378", "board": "Scope", "end_date": end},
        {"id": 42, "name": "Chupacabra 378", "board": "AWS & Content", "end_date": end},
    ])
    html = _run(tmp_workspace, monkeypatch, signals)
    assert "Scope" in html
    assert "AWS &amp; Content" in html
    assert "shared" in html  # second row says "shared: ..."


def test_render_person_rows_via_brief(tmp_workspace, monkeypatch):
    """Per-person status comes from the brief narrative, not a separate table."""
    brief = "*Alice* — working on PROJ-1\n*Bob* — working on PROJ-2\n"
    html = _run_with_brief(tmp_workspace, monkeypatch, _make_signals(), brief_md=brief)
    assert "Alice" in html
    assert "Bob" in html


def test_render_stale_via_brief(tmp_workspace, monkeypatch):
    """Stale indicators surface through the synthesized brief."""
    brief = "*Carol* — PROJ-9 stale (no update in 3 days)\n"
    html = _run_with_brief(tmp_workspace, monkeypatch, _make_signals(), brief_md=brief)
    assert "stale" in html


def test_render_includes_risk_report(tmp_workspace, monkeypatch):
    risk_md = "## Risks\n\n- 🔴 HIGH: something bad\n- 🟡 MED: medium thing\n"
    html = _run(tmp_workspace, monkeypatch, _make_signals(), risk_md=risk_md)
    assert "something bad" in html
    assert "medium thing" in html


def test_render_missing_signals_exits_1(tmp_workspace, monkeypatch):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    import importlib
    import scripts.standup.render_html as mod
    importlib.reload(mod)
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 1


def _run_with_brief(tmp_workspace, monkeypatch, signals, brief_md="", risk_md=""):
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    (tmp_workspace / "signals.json").write_text(json.dumps(signals))
    if brief_md:
        (tmp_workspace / "standup_brief.md").write_text(brief_md)
    if risk_md:
        (tmp_workspace / "risk_report.md").write_text(risk_md)

    import importlib
    import scripts.standup.render_html as mod
    importlib.reload(mod)

    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 0
    return (tmp_workspace / "reports" / "report.html").read_text()


def test_brief_included_in_html(tmp_workspace, monkeypatch):
    brief = "## Call to Action\n\nAll 8 PRs have zero reviewers.\n\n## Discussion Questions\n\n1. Who reviews PR #123 today?\n"
    html = _run_with_brief(tmp_workspace, monkeypatch, _make_signals(), brief_md=brief)
    assert "Call to Action" in html
    assert "zero reviewers" in html
    assert "Discussion Questions" in html
    assert "Who reviews PR #123 today?" in html
    assert "brief-section" in html


def test_brief_absent_renders_without_section(tmp_workspace, monkeypatch):
    html = _run_with_brief(tmp_workspace, monkeypatch, _make_signals())
    assert '<div class="brief-section">' not in html


def test_emoji_codes_converted(tmp_workspace, monkeypatch):
    brief = ":rotating_light: Urgent!\n:bust_in_silhouette: Per-person\n:question: Discussion\n"
    html = _run_with_brief(tmp_workspace, monkeypatch, _make_signals(), brief_md=brief)
    assert "🚨" in html
    assert "👤" in html
    assert "❓" in html


def test_brief_slack_bold_rendered(tmp_workspace, monkeypatch):
    """Slack *bold* (single asterisk) should become <strong>."""
    brief = "*Alice* — working on PROJ-1\n"
    html = _run_with_brief(tmp_workspace, monkeypatch, _make_signals(), brief_md=brief)
    assert "<strong>Alice</strong>" in html


def test_mrkdwn_links_rendered_as_anchors(tmp_workspace, monkeypatch):
    """Slack <url|text> links in standup_brief.md should become clickable <a> tags."""
    brief = "🔴 <https://jira.com/browse/CRIBL-123|CRIBL-123> blocked\n"
    html = _run_with_brief(tmp_workspace, monkeypatch, _make_signals(), brief_md=brief)
    assert '<a href="https://jira.com/browse/CRIBL-123">CRIBL-123</a>' in html
    assert "&lt;https://jira.com" not in html
