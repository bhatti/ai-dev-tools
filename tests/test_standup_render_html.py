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
    return (tmp_workspace / "standup_report.html").read_text()


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


def test_render_person_rows(tmp_workspace, monkeypatch):
    issues = [
        {"key": "PROJ-1", "summary": "Fix bug", "status": "Done",
         "assignee": "Alice", "is_stale": False, "url": "https://jira/PROJ-1"},
        {"key": "PROJ-2", "summary": "New feature", "status": "In Progress",
         "assignee": "Bob", "is_stale": False, "url": "https://jira/PROJ-2"},
    ]
    html = _run(tmp_workspace, monkeypatch, _make_signals(issues=issues))
    assert "Alice" in html
    assert "Bob" in html
    assert "PROJ-1" in html
    assert "PROJ-2" in html


def test_render_stale_badge(tmp_workspace, monkeypatch):
    issues = [
        {"key": "PROJ-9", "summary": "Stale item", "status": "In Progress",
         "assignee": "Carol", "is_stale": True, "url": ""},
    ]
    html = _run(tmp_workspace, monkeypatch, _make_signals(issues=issues))
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
