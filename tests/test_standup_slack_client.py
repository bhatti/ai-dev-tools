"""Tests for scripts/standup/slack_client.py"""

from unittest.mock import MagicMock, patch

import pytest

from scripts.standup.slack_client import get_standup_messages, notify, post_message, resolve_channel_id


@pytest.fixture
def config_with_slack(tmp_workspace):
    return {
        "WORKSPACE_DIR": str(tmp_workspace),
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_STANDUP_CHANNEL": "standup",
    }


@pytest.fixture
def config_no_slack(tmp_workspace):
    return {
        "WORKSPACE_DIR": str(tmp_workspace),
    }


# ---------------------------------------------------------------------------
# resolve_channel_id
# ---------------------------------------------------------------------------

@patch("scripts.standup.slack_client.requests.get")
def test_resolve_channel_id_found(mock_get):
    mock_get.return_value = MagicMock(
        ok=True,
        json=lambda: {
            "ok": True,
            "channels": [{"id": "C123", "name": "standup"}],
            "response_metadata": {"next_cursor": ""},
        },
    )
    result = resolve_channel_id("xoxb-test", "standup")
    assert result == "C123"


@patch("scripts.standup.slack_client.requests.get")
def test_resolve_channel_id_not_found(mock_get):
    mock_get.return_value = MagicMock(
        ok=True,
        json=lambda: {
            "ok": True,
            "channels": [{"id": "C999", "name": "general"}],
            "response_metadata": {"next_cursor": ""},
        },
    )
    result = resolve_channel_id("xoxb-test", "standup")
    assert result is None


# ---------------------------------------------------------------------------
# get_standup_messages
# ---------------------------------------------------------------------------

def test_get_standup_messages_no_token(config_no_slack):
    msgs = get_standup_messages(config_no_slack)
    assert msgs == []


@patch("scripts.standup.slack_client.requests.get")
def test_get_standup_messages_with_blocker_keyword(mock_get, config_with_slack):
    # First call: conversations.list; second call: conversations.history
    mock_get.side_effect = [
        MagicMock(ok=True, json=lambda: {
            "ok": True,
            "channels": [{"id": "C123", "name": "standup"}],
            "response_metadata": {"next_cursor": ""},
        }),
        MagicMock(ok=True, json=lambda: {
            "ok": True,
            "messages": [
                {"user": "U1", "text": "I'm blocked on auth ticket", "ts": "1700000000.0"},
                {"user": "U2", "text": "Deployed feature X", "ts": "1700000001.0"},
            ],
        }),
    ]
    msgs = get_standup_messages(config_with_slack)
    assert len(msgs) == 2
    blocker_msgs = [m for m in msgs if m["has_blocker_keyword"]]
    assert len(blocker_msgs) == 1
    assert blocker_msgs[0]["user"] == "U1"


# ---------------------------------------------------------------------------
# post_message
# ---------------------------------------------------------------------------

def test_post_message_no_token(config_no_slack):
    ok = post_message(config_no_slack, "hello")
    assert ok is False


@patch("scripts.standup.slack_client.requests.post")
def test_post_message_success(mock_post, config_with_slack):
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": True})
    ok = post_message(config_with_slack, "📋 *Standup Brief*")
    assert ok is True
    assert mock_post.call_args.kwargs["json"]["channel"] == "#standup"


@patch("scripts.standup.slack_client.requests.post")
def test_post_message_slack_error(mock_post, config_with_slack):
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": False, "error": "channel_not_found"})
    ok = post_message(config_with_slack, "hello")
    assert ok is False


# ---------------------------------------------------------------------------
# notify
# ---------------------------------------------------------------------------

def test_notify_no_token(config_no_slack):
    ok = notify(config_no_slack, "hello")
    assert ok is False


def test_notify_no_channel(tmp_workspace):
    config = {"WORKSPACE_DIR": str(tmp_workspace), "SLACK_BOT_TOKEN": "xoxb-test"}
    ok = notify(config, "hello")
    assert ok is False


@patch("scripts.standup.slack_client.requests.post")
def test_notify_success(mock_post, tmp_workspace):
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": True})
    config = {
        "WORKSPACE_DIR": str(tmp_workspace),
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL": "my-team",
    }
    ok = notify(config, "🤖 PR created: https://github.com/org/repo/pull/1")
    assert ok is True
    assert mock_post.call_args.kwargs["json"]["channel"] == "#my-team"


@patch("scripts.standup.slack_client.requests.post")
def test_notify_custom_channel_key(mock_post, tmp_workspace):
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": True})
    config = {
        "WORKSPACE_DIR": str(tmp_workspace),
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_STANDUP_CHANNEL": "standup-alerts",
    }
    ok = notify(config, "✅ PR merged", channel_key="SLACK_STANDUP_CHANNEL")
    assert ok is True
    assert mock_post.call_args.kwargs["json"]["channel"] == "#standup-alerts"
