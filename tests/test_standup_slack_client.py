"""Tests for scripts/standup/slack_client.py"""

from unittest.mock import MagicMock, patch

import pytest

from scripts.standup.slack_client import (
    build_issue_blocks, build_mrkdwn_blocks, build_pr_blocks,
    get_standup_messages, notify, post_message, resolve_channel_id, upload_file,
)


@pytest.fixture
def config_with_slack(tmp_workspace):
    return {
        "WORKSPACE_DIR": str(tmp_workspace),
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL": "standup",
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
    assert mock_post.call_args.kwargs["json"]["channel"] == "standup"


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
    assert mock_post.call_args.kwargs["json"]["channel"] == "my-team"


@patch("scripts.standup.slack_client.requests.post")
def test_notify_posts_to_thread_when_slack_thread_ts_set(mock_post, tmp_workspace):
    """notify() replies in the originating thread when SlackThreadTs is in config."""
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": True})
    config = {
        "WORKSPACE_DIR": str(tmp_workspace),
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL": "my-team",
        "SlackThreadTs": "1785862519.738719",
    }
    ok = notify(config, "🤖 PR created: https://bitbucket.org/org/repo/pull-requests/123")
    assert ok is True
    payload = mock_post.call_args.kwargs["json"]
    assert payload["channel"] == "my-team"
    assert payload.get("thread_ts") == "1785862519.738719"


@patch("scripts.standup.slack_client.requests.post")
def test_notify_no_thread_when_ts_absent(mock_post, tmp_workspace):
    """notify() posts to channel root when no thread timestamp is set."""
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": True})
    config = {
        "WORKSPACE_DIR": str(tmp_workspace),
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL": "my-team",
    }
    ok = notify(config, "🤖 PR created")
    assert ok is True
    payload = mock_post.call_args.kwargs["json"]
    assert "thread_ts" not in payload


@patch("scripts.standup.slack_client.requests.post")
def test_notify_custom_channel_key(mock_post, tmp_workspace):
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": True})
    config = {
        "WORKSPACE_DIR": str(tmp_workspace),
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL": "standup-alerts",
    }
    ok = notify(config, "✅ PR merged", channel_key="SLACK_CHANNEL")
    assert ok is True
    assert mock_post.call_args.kwargs["json"]["channel"] == "standup-alerts"


@patch("scripts.standup.slack_client.requests.post")
def test_notify_with_blocks(mock_post, tmp_workspace):
    """notify() passes blocks to post_message when provided."""
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": True})
    config = {
        "WORKSPACE_DIR": str(tmp_workspace),
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL": "my-team",
    }
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*Title*"}}]
    ok = notify(config, "fallback text", blocks=blocks)
    assert ok is True
    payload = mock_post.call_args.kwargs["json"]
    assert payload["blocks"] == blocks
    assert payload["text"] == "fallback text"


@patch("scripts.standup.slack_client.requests.post")
def test_post_message_no_blocks_by_default(mock_post, tmp_workspace):
    """post_message() does not include blocks key when blocks=None."""
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": True})
    config = {"SLACK_BOT_TOKEN": "xoxb-test", "SLACK_CHANNEL": "c"}
    post_message(config, "hello")
    payload = mock_post.call_args.kwargs["json"]
    assert "blocks" not in payload


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------

def test_upload_file_no_token(tmp_workspace, config_no_slack):
    (tmp_workspace / "report.html").write_text("<html/>")
    ok = upload_file(config_no_slack, str(tmp_workspace / "report.html"), "report.html")
    assert ok is False


@patch("scripts.standup.slack_client.requests.get")
@patch("scripts.standup.slack_client.requests.put")
@patch("scripts.standup.slack_client.requests.post")
def test_upload_file_success(mock_post, mock_put, mock_get, config_with_slack, tmp_workspace):
    html_file = tmp_workspace / "report.html"
    html_file.write_text("<html><body>Report</body></html>")

    # conversations.list for resolve_channel_id
    mock_get.return_value = MagicMock(ok=True, json=lambda: {
        "ok": True,
        "channels": [{"id": "C123", "name": "standup"}],
        "response_metadata": {"next_cursor": ""},
    })
    # getUploadURLExternal then completeUploadExternal
    mock_post.side_effect = [
        MagicMock(ok=True, json=lambda: {
            "ok": True, "upload_url": "https://files.slack.com/upload/v1/xyz", "file_id": "F123"
        }),
        MagicMock(ok=True, json=lambda: {"ok": True}),
    ]
    mock_put.return_value = MagicMock(ok=True)

    ok = upload_file(config_with_slack, str(html_file), "report.html",
                     initial_comment="Full report")
    assert ok is True
    # PUT called with the upload URL
    assert mock_put.call_args.args[0] == "https://files.slack.com/upload/v1/xyz"
    # completeUploadExternal called with channel_id and file_id
    complete_payload = mock_post.call_args_list[1].kwargs["json"]
    assert complete_payload["channel_id"] == "C123"
    assert complete_payload["files"][0]["id"] == "F123"
    assert complete_payload["initial_comment"] == "Full report"


@patch("scripts.standup.slack_client.requests.post")
def test_upload_file_get_url_fails(mock_post, config_with_slack, tmp_workspace):
    html_file = tmp_workspace / "report.html"
    html_file.write_text("<html/>")
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": False, "error": "not_allowed"})
    ok = upload_file(config_with_slack, str(html_file), "report.html")
    assert ok is False


# ---------------------------------------------------------------------------
# build_mrkdwn_blocks
# ---------------------------------------------------------------------------

def test_build_mrkdwn_blocks_basic():
    text = "Line one\nLine two\n\nParagraph two\nLine three"
    blocks = build_mrkdwn_blocks(text)
    # Should produce section blocks + divider
    types = [b["type"] for b in blocks]
    assert "section" in types
    assert types[-1] == "divider"
    # All text content should be mrkdwn
    for b in blocks:
        if b["type"] == "section":
            assert b["text"]["type"] == "mrkdwn"


def test_build_mrkdwn_blocks_splits_long_text():
    # Build a text longer than 2900 chars in a single paragraph
    long_para_a = "A " * 1000  # 2000 chars
    long_para_b = "B " * 1000  # 2000 chars
    text = long_para_a + "\n\n" + long_para_b
    blocks = build_mrkdwn_blocks(text)
    section_blocks = [b for b in blocks if b["type"] == "section"]
    # Should split into at least 2 sections
    assert len(section_blocks) >= 2


# ---------------------------------------------------------------------------
# build_pr_blocks
# ---------------------------------------------------------------------------

def test_build_pr_blocks_empty():
    blocks = build_pr_blocks("PR Queue", {"sprint": "Sprint 5", "pr_count": 0, "prs": []})
    texts = [b.get("text", {}).get("text", "") for b in blocks]
    assert any("No open PRs" in t for t in texts)


def test_build_pr_blocks_with_prs():
    pr_data = {
        "sprint": "Sprint 5",
        "pr_count": 2,
        "prs": [
            {
                "id": "42",
                "jira_key": "PROJ-100",
                "title": "Fix bug",
                "jira_summary": "Fix important bug",
                "url": "https://github.com/org/repo/pull/42",
                "jira_url": "https://org.atlassian.net/browse/PROJ-100",
                "author": "Alice Smith",
                "age_days": 2,
                "approved_by": ["Bob Jones"],
                "reviewers": ["Charlie"],
                "status": "In Review",
            },
            {
                "id": "10",
                "jira_key": "PROJ-200",
                "title": "Stale PR",
                "jira_summary": "Old work",
                "url": "https://github.com/org/repo/pull/10",
                "jira_url": "https://org.atlassian.net/browse/PROJ-200",
                "author": "Dave",
                "age_days": 8,
                "approved_by": [],
                "reviewers": [],
                "status": "Open",
            },
        ],
    }
    blocks = build_pr_blocks("Sprint PR Queue", pr_data)
    # Header block present
    assert blocks[0]["type"] == "header"
    # Both PRs appear as clickable links somewhere in text content
    all_text = " ".join(
        b.get("text", {}).get("text", "") for b in blocks if b["type"] == "section"
    )
    all_fields = " ".join(
        f.get("text", "") for b in blocks if b["type"] == "section"
        for f in (b.get("fields") or [])
    )
    assert "PROJ-100" in all_text
    assert "PROJ-200" in all_text
    assert "pull/42" in all_text
    # Approved PR should have approved-by info in fields
    assert "Bob" in all_fields
