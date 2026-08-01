"""Tests for scripts/check_connectivity.py"""

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CREDENTIAL_VARS = [
    "GH_TOKEN", "GH_ORG", "GH_REPO",
    "JIRA_API_TOKEN", "JIRA_EMAIL", "JIRA_BASE_URL",
    "BITBUCKET_TOKEN", "BITBUCKET_USERNAME", "BITBUCKET_WORKSPACE", "BITBUCKET_REPO",
    "SLACK_BOT_TOKEN", "SLACK_STANDUP_CHANNEL", "SLACK_CHANNEL",
    "CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_API_KEY", "ANTHROPIC_BEDROCK_BASE_URL",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
]


def _run_main(monkeypatch, tmp_workspace, env: dict):
    # Wipe all credential vars so real local credentials don't bleed into tests
    for var in _CREDENTIAL_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_workspace))
    for k, v in env.items():
        monkeypatch.setenv(k, v)

    # Re-import to pick up fresh env; also reset module-level _results list
    import importlib
    import scripts.check_connectivity as mod
    importlib.reload(mod)

    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 0  # always exits 0

    result_path = tmp_workspace / "connectivity_result.json"
    assert result_path.exists()
    return json.loads(result_path.read_text())


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

@patch("scripts.check_connectivity.requests.get")
def test_github_ok(mock_get, tmp_workspace, monkeypatch):
    mock_get.return_value = MagicMock(
        status_code=200, ok=True,
        json=lambda: {"login": "ai-bot"},
    )
    result = _run_main(monkeypatch, tmp_workspace, {"GH_TOKEN": "ghp_test"})
    gh = next(r for r in result["checks"] if r["check"] == "github")
    assert gh["status"] == "OK"
    assert "ai-bot" in gh["detail"]


def test_github_skip(tmp_workspace, monkeypatch):
    result = _run_main(monkeypatch, tmp_workspace, {})
    gh = next(r for r in result["checks"] if r["check"] == "github")
    assert gh["status"] == "SKIP"


@patch("scripts.check_connectivity.requests.get")
def test_github_fail(mock_get, tmp_workspace, monkeypatch):
    mock_get.return_value = MagicMock(status_code=401, ok=False, text="Unauthorized")
    result = _run_main(monkeypatch, tmp_workspace, {"GH_TOKEN": "bad-token"})
    gh = next(r for r in result["checks"] if r["check"] == "github")
    assert gh["status"] == "FAIL"
    assert "401" in gh["detail"]


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------

@patch("scripts.check_connectivity.requests.get")
def test_jira_ok(mock_get, tmp_workspace, monkeypatch):
    mock_get.return_value = MagicMock(
        status_code=200, ok=True,
        json=lambda: {"displayName": "Alice"},
    )
    result = _run_main(monkeypatch, tmp_workspace, {
        "JIRA_API_TOKEN": "tok", "JIRA_EMAIL": "a@b.com",
        "JIRA_BASE_URL": "https://example.atlassian.net",
    })
    jira = next(r for r in result["checks"] if r["check"] == "jira")
    assert jira["status"] == "OK"
    assert "Alice" in jira["detail"]


def test_jira_skip(tmp_workspace, monkeypatch):
    result = _run_main(monkeypatch, tmp_workspace, {})
    jira = next(r for r in result["checks"] if r["check"] == "jira")
    assert jira["status"] == "SKIP"


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

@patch("scripts.check_connectivity.requests.get")
def test_slack_ok(mock_get, tmp_workspace, monkeypatch):
    mock_get.return_value = MagicMock(
        ok=True,
        json=lambda: {"ok": True, "user": "ai-bot", "team": "Acme"},
    )
    result = _run_main(monkeypatch, tmp_workspace, {"SLACK_BOT_TOKEN": "xoxb-test"})
    slack = next(r for r in result["checks"] if r["check"] == "slack")
    assert slack["status"] == "OK"
    assert "ai-bot" in slack["detail"]


def test_slack_skip(tmp_workspace, monkeypatch):
    result = _run_main(monkeypatch, tmp_workspace, {})
    slack = next(r for r in result["checks"] if r["check"] == "slack")
    assert slack["status"] == "SKIP"


@patch("scripts.check_connectivity.requests.get")
def test_slack_fail(mock_get, tmp_workspace, monkeypatch):
    mock_get.return_value = MagicMock(
        ok=True,
        json=lambda: {"ok": False, "error": "invalid_auth"},
    )
    result = _run_main(monkeypatch, tmp_workspace, {"SLACK_BOT_TOKEN": "bad"})
    slack = next(r for r in result["checks"] if r["check"] == "slack")
    assert slack["status"] == "FAIL"
    assert "invalid_auth" in slack["detail"]


# ---------------------------------------------------------------------------
# Claude (Bedrock)
# ---------------------------------------------------------------------------

@patch("scripts.check_connectivity.requests.post")
@patch("scripts.check_connectivity.requests.get")
def test_claude_bedrock_ok(mock_get, mock_post, tmp_workspace, monkeypatch):
    mock_get.return_value = MagicMock(
        status_code=200, ok=True,
        json=lambda: {"models": [
            {"id": "us.anthropic.claude-sonnet-4-6"},
            {"id": "us.anthropic.claude-opus-4-6-v1"},
        ]},
    )
    mock_post.return_value = MagicMock(
        status_code=200, ok=True,
        json=lambda: {"content": [{"text": "OK"}]},
    )
    result = _run_main(monkeypatch, tmp_workspace, {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "ANTHROPIC_BEDROCK_BASE_URL": "http://ai/bedrock",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
    })
    models_check = next(r for r in result["checks"] if r["check"] == "claude-models")
    assert models_check["status"] == "OK"
    assert "2 models" in models_check["detail"]
    claude = next(r for r in result["checks"] if r["check"] == "claude-bedrock")
    assert claude["status"] == "OK"
    assert "OK" in claude["detail"]


@patch("scripts.check_connectivity.requests.post")
@patch("scripts.check_connectivity.requests.get")
def test_claude_bedrock_fail(mock_get, mock_post, tmp_workspace, monkeypatch):
    mock_get.return_value = MagicMock(
        status_code=200, ok=True,
        json=lambda: {"models": [{"id": "us.anthropic.claude-sonnet-4-6"}]},
    )
    mock_post.return_value = MagicMock(
        status_code=500, ok=False, text="Internal Server Error",
    )
    result = _run_main(monkeypatch, tmp_workspace, {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "ANTHROPIC_BEDROCK_BASE_URL": "http://ai/bedrock",
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
    })
    claude = next(r for r in result["checks"] if r["check"] == "claude-bedrock")
    assert claude["status"] == "FAIL"
    assert "500" in claude["detail"]


def test_claude_skip(tmp_workspace, monkeypatch):
    result = _run_main(monkeypatch, tmp_workspace, {})
    claude = next(r for r in result["checks"] if "claude" in r["check"] and r["check"] != "claude-cli")
    assert claude["status"] == "SKIP"


# ---------------------------------------------------------------------------
# Summary shape
# ---------------------------------------------------------------------------

def test_summary_shape(tmp_workspace, monkeypatch):
    result = _run_main(monkeypatch, tmp_workspace, {})
    assert "timestamp" in result
    assert "ok" in result
    assert "fail" in result
    assert "skip" in result
    assert isinstance(result["checks"], list)
    assert result["fail"] == 0   # no creds set → all skipped, none failed
