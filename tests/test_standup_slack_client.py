"""Tests for scripts/standup/slack_client.py"""

from unittest.mock import MagicMock, patch

import pytest

import scripts.standup.slack_client as _sc
from scripts.standup.slack_client import (
    build_issue_blocks, build_mrkdwn_blocks, build_pr_blocks,
    get_standup_messages, notify, post_message, post_report, upload_html_report,
    resolve_channel_id, upload_file, pr_group,
)
from scripts.adhoc.run_skill import _pr_queue_to_markdown, _write_pr_queue_report


@pytest.fixture(autouse=True)
def _clear_channel_cache():
    """Clear the module-level channel ID cache between tests to prevent cross-test pollution."""
    _sc._channel_id_cache.clear()
    yield
    _sc._channel_id_cache.clear()


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
    mock_post.return_value = MagicMock(
        ok=True, json=lambda: {"ok": True, "ts": "1700000001.000100"}
    )
    ok = post_message(config_with_slack, "📋 *Standup Brief*")
    assert ok is True
    assert mock_post.call_args.kwargs["json"]["channel"] == "standup"


@patch("scripts.standup.slack_client.requests.post")
def test_post_message_slack_error(mock_post, config_with_slack):
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": False, "error": "channel_not_found"})
    ok = post_message(config_with_slack, "hello")
    assert ok is False


@patch("scripts.standup.slack_client.requests.post")
def test_post_message_success_missing_ts_still_returns_true(mock_post, config_with_slack):
    """post_message returns True even when response lacks ts (shouldn't happen, but defensive)."""
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": True})
    ok = post_message(config_with_slack, "hello")
    assert ok is True


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


def test_build_pr_blocks_shows_priority_and_labels():
    """High priority shows 🟠 emoji; labels appear in the reviewer field."""
    pr_data = {
        "sprint": "Sprint 5",
        "pr_count": 1,
        "prs": [
            {
                "id": "77",
                "jira_key": "PROJ-300",
                "title": "High prio fix",
                "jira_summary": "Critical path fix",
                "url": "https://github.com/org/repo/pull/77",
                "jira_url": "https://org.atlassian.net/browse/PROJ-300",
                "author": "Eve",
                "age_days": 2,
                "approved_by": [],
                "reviewers": ["Frank"],
                "priority": "High",
                "labels": ["2609-release", "backend"],
            }
        ],
    }
    blocks = build_pr_blocks("PR Queue", pr_data)
    all_text = " ".join(b.get("text", {}).get("text", "") for b in blocks if b["type"] == "section")
    all_fields = " ".join(
        f.get("text", "") for b in blocks if b["type"] == "section"
        for f in (b.get("fields") or [])
    )
    assert "🟠" in all_text
    assert "`2609-release`" in all_fields
    assert "`backend`" in all_fields


# ---------------------------------------------------------------------------
# upload_file with thread_ts
# ---------------------------------------------------------------------------

@patch("scripts.standup.slack_client.requests.get")
@patch("scripts.standup.slack_client.requests.put")
@patch("scripts.standup.slack_client.requests.post")
def test_upload_file_thread_ts_forwarded(mock_post, mock_put, mock_get, config_with_slack, tmp_workspace):
    """thread_ts is included in the completeUploadExternal payload."""
    html_file = tmp_workspace / "report.html"
    html_file.write_text("<html/>")

    mock_get.return_value = MagicMock(ok=True, json=lambda: {
        "ok": True,
        "channels": [{"id": "C123", "name": "standup"}],
        "response_metadata": {"next_cursor": ""},
    })
    mock_post.side_effect = [
        MagicMock(ok=True, json=lambda: {"ok": True, "upload_url": "https://upload.example/v1", "file_id": "F999"}),
        MagicMock(ok=True, json=lambda: {"ok": True}),
    ]
    mock_put.return_value = MagicMock(ok=True)

    ok = upload_file(config_with_slack, str(html_file), "audit_report.html",
                     thread_ts="1700000001.000100")
    assert ok is True
    complete_payload = mock_post.call_args_list[1].kwargs["json"]
    assert complete_payload.get("thread_ts") == "1700000001.000100"


@patch("scripts.standup.slack_client.requests.get")
@patch("scripts.standup.slack_client.requests.put")
@patch("scripts.standup.slack_client.requests.post")
def test_upload_file_no_thread_ts(mock_post, mock_put, mock_get, config_with_slack, tmp_workspace):
    """thread_ts is omitted from completeUploadExternal when not provided."""
    html_file = tmp_workspace / "report.html"
    html_file.write_text("<html/>")

    mock_get.return_value = MagicMock(ok=True, json=lambda: {
        "ok": True,
        "channels": [{"id": "C123", "name": "standup"}],
        "response_metadata": {"next_cursor": ""},
    })
    mock_post.side_effect = [
        MagicMock(ok=True, json=lambda: {"ok": True, "upload_url": "https://upload.example/v1", "file_id": "F000"}),
        MagicMock(ok=True, json=lambda: {"ok": True}),
    ]
    mock_put.return_value = MagicMock(ok=True)

    ok = upload_file(config_with_slack, str(html_file), "audit_report.html")
    assert ok is True
    complete_payload = mock_post.call_args_list[1].kwargs["json"]
    assert "thread_ts" not in complete_payload


# ---------------------------------------------------------------------------
# post_report
# ---------------------------------------------------------------------------

@patch("scripts.standup.slack_client.upload_file", return_value=True)
@patch("scripts.common.report_renderer.render_simple_html", return_value="<html/>")
@patch("scripts.standup.slack_client.requests.post")
def test_post_report_success(mock_post, mock_render, mock_upload, config_with_slack):
    """post_report posts text, renders HTML, uploads file, returns True."""
    mock_post.return_value = MagicMock(
        ok=True, json=lambda: {"ok": True, "ts": "1700000005.000200"}
    )

    ok = post_report(config_with_slack, "slack text", "# md text",
                     title="My Report", filename="report.html")
    assert ok is True
    mock_render.assert_called_once_with("My Report", "# md text")
    assert mock_upload.call_count == 1
    # HTML must be threaded to the new message's ts (no existing thread)
    _, upload_kwargs = mock_upload.call_args
    assert upload_kwargs.get("thread_ts") == "1700000005.000200"


@patch("scripts.standup.slack_client.upload_file", return_value=True)
@patch("scripts.common.report_renderer.render_simple_html", return_value="<html/>")
@patch("scripts.standup.slack_client.requests.post")
def test_post_report_existing_thread_ts(mock_post, mock_render, mock_upload, config_with_slack):
    """When an existing thread_ts is passed, the upload threads to that ts."""
    mock_post.return_value = MagicMock(
        ok=True, json=lambda: {"ok": True, "ts": "1700000009.000001"}
    )

    ok = post_report(config_with_slack, "slack text", "# md",
                     title="Report", filename="report.html",
                     thread_ts="1700000001.000001")
    assert ok is True
    _, upload_kwargs = mock_upload.call_args
    assert upload_kwargs.get("thread_ts") == "1700000001.000001"


@patch("scripts.standup.slack_client.requests.post")
def test_post_report_text_fails_returns_false(mock_post, config_with_slack):
    """post_report returns False when the text post fails."""
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": False, "error": "channel_not_found"})
    ok = post_report(config_with_slack, "text", "# md",
                     title="Report", filename="report.html")
    assert ok is False


@patch("scripts.standup.slack_client.upload_file", return_value=False)
@patch("scripts.common.report_renderer.render_simple_html", return_value="<html/>")
@patch("scripts.standup.slack_client.requests.post")
def test_post_report_upload_fails_is_nonfatal(mock_post, mock_render, mock_upload, config_with_slack):
    """Upload failure is non-fatal — post_report still returns True."""
    mock_post.return_value = MagicMock(
        ok=True, json=lambda: {"ok": True, "ts": "1700000010.000001"}
    )

    ok = post_report(config_with_slack, "text", "# md",
                     title="Report", filename="report.html")
    assert ok is True  # text posted OK → True despite upload failure


@patch("scripts.common.report_renderer.render_simple_html", side_effect=RuntimeError("render error"))
@patch("scripts.standup.slack_client.requests.post")
def test_post_report_render_exception_is_nonfatal(mock_post, mock_render, config_with_slack):
    """HTML render exception is non-fatal — post_report still returns True."""
    mock_post.return_value = MagicMock(
        ok=True, json=lambda: {"ok": True, "ts": "1700000011.000001"}
    )

    ok = post_report(config_with_slack, "text", "# md",
                     title="Report", filename="report.html")
    assert ok is True


# ---------------------------------------------------------------------------
# post_report — fallback link when upload fails
# ---------------------------------------------------------------------------

@patch("scripts.standup.slack_client.upload_file", return_value=False)
@patch("scripts.common.report_renderer.render_simple_html", return_value="<html/>")
@patch("scripts.standup.slack_client.requests.post")
def test_post_report_fallback_uses_by_job_link(mock_post, mock_render, mock_upload):
    """When Slack upload fails, fallback posts the by-job formicary artifact link."""
    mock_post.return_value = MagicMock(
        ok=True, json=lambda: {"ok": True, "ts": "1700000020.000001"}
    )
    config = {
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL": "#test",
        "FORMICARY_PUBLIC_URL": "https://formicary.example.com",
        "JOB_ID": "job-abc-123",
    }

    post_report(config, "text", "# md",
                title="PR Audit", filename="pr_audit_report.html")

    # Two posts: main message + fallback
    assert mock_post.call_count == 2
    fallback_text = mock_post.call_args_list[1].kwargs["json"].get("text", "")
    # by-job endpoint in fallback
    assert "by-job" in fallback_text
    assert "pr_audit_report.html" in fallback_text
    # Job page link for "All artifacts"
    assert "dashboard/jobs/requests/job-abc-123" in fallback_text


@patch("scripts.standup.slack_client.upload_file", return_value=False)
@patch("scripts.common.report_renderer.render_simple_html", return_value="<html/>")
@patch("scripts.standup.slack_client.requests.post")
def test_post_report_fallback_posts_by_job_link(mock_post, mock_render, mock_upload):
    """Fallback posts the by-job artifact endpoint link."""
    mock_post.return_value = MagicMock(
        ok=True, json=lambda: {"ok": True, "ts": "1700000021.000001"}
    )
    config = {
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL": "#test",
        "FORMICARY_PUBLIC_URL": "https://formicary.example.com",
        "JOB_ID": "job-xyz-456",
    }

    post_report(config, "text", "# md",
                title="Standup", filename="standup_report.html")

    assert mock_post.call_count == 2
    fallback_text = mock_post.call_args_list[1].kwargs["json"].get("text", "")
    # by-job endpoint with default task_type="post"
    assert "by-job" in fallback_text
    assert "task=post" in fallback_text
    assert "file=reports/standup_report.html" in fallback_text
    # Job page link for "All artifacts"
    assert "dashboard/jobs/requests/job-xyz-456" in fallback_text


@patch("scripts.standup.slack_client.upload_file", return_value=False)
@patch("scripts.common.report_renderer.render_simple_html", return_value="<html/>")
@patch("scripts.standup.slack_client.requests.post")
def test_post_report_fallback_uses_caller_task_type(mock_post, mock_render, mock_upload):
    """Fallback uses the task_type passed by the caller, not a hardcoded default."""
    mock_post.return_value = MagicMock(
        ok=True, json=lambda: {"ok": True, "ts": "1700000025.000001"}
    )
    config = {
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL": "#test",
        "FORMICARY_PUBLIC_URL": "https://formicary.example.com",
        "JOB_ID": "job-audit-789",
    }

    post_report(config, "text", "# md",
                title="PR Audit", filename="pr_audit_report.html",
                task_type="audit-prs")

    assert mock_post.call_count == 2
    fallback_text = mock_post.call_args_list[1].kwargs["json"].get("text", "")
    assert "task=audit-prs" in fallback_text
    assert "file=reports/pr_audit_report.html" in fallback_text


@patch("scripts.standup.slack_client.upload_file", return_value=False)
@patch("scripts.common.report_renderer.render_simple_html", return_value="<html/>")
@patch("scripts.standup.slack_client.requests.post")
def test_post_report_no_fallback_when_no_job_id(mock_post, mock_render, mock_upload):
    """Fallback link is skipped when JOB_ID is absent."""
    mock_post.return_value = MagicMock(
        ok=True, json=lambda: {"ok": True, "ts": "1700000022.000001"}
    )
    config = {
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL": "#test",
        "FORMICARY_PUBLIC_URL": "https://formicary.example.com",
        # JOB_ID intentionally absent
    }

    post_report(config, "text", "# md", title="Report", filename="report.html")

    # Only 1 post call (the main text message) — no fallback
    assert mock_post.call_count == 1


# ---------------------------------------------------------------------------
# _pr_group — module-level grouping logic (shared by Slack blocks + Markdown)
# ---------------------------------------------------------------------------

def test_pr_group_ci_failure_overrides_approvals():
    pr = {"ci_status": "failure", "approval_count": 2, "age_days": 0}
    assert pr_group(pr) == "CI FAILING"


def test_pr_group_ready_to_merge_two_approvals():
    assert pr_group({"ci_status": "success", "approval_count": 2}) == "READY TO MERGE"
    assert pr_group({"ci_status": "none", "approval_count": 2}) == "READY TO MERGE"


def test_pr_group_approved_waiting_on_ci():
    assert pr_group({"ci_status": "pending", "approval_count": 2}) == "APPROVED — WAITING ON CI"


def test_pr_group_one_approval():
    assert pr_group({"ci_status": "none", "approval_count": 1, "age_days": 0}) == "APPROVED (1 review)"


def test_pr_group_stale():
    assert pr_group({"ci_status": "none", "approval_count": 0, "age_days": 6}) == "STALE / AT RISK (>5d)"


def test_pr_group_needs_review():
    assert pr_group({"ci_status": "none", "approval_count": 0, "age_days": 2}) == "NEEDS REVIEW (>1d)"


def test_pr_group_in_review():
    assert pr_group({"ci_status": "none", "approval_count": 0, "age_days": 1}) == "IN REVIEW"


def test_pr_group_fallback_approved_by_list():
    """Falls back to len(approved_by) when approval_count is absent."""
    pr = {"approved_by": ["Alice", "Bob"], "age_days": 0}
    assert pr_group(pr) == "READY TO MERGE"


# ---------------------------------------------------------------------------
# upload_html_report
# ---------------------------------------------------------------------------

@patch("scripts.standup.slack_client.upload_file", return_value=True)
@patch("scripts.standup.slack_client.requests.post")
def test_upload_html_report_success(mock_post, mock_upload, config_with_slack):
    """Returns True and calls upload_file when html_content is provided."""
    ok = upload_html_report(config_with_slack, "<html>hi</html>", "report.html",
                            thread_ts="1700000001.000001")
    assert ok is True
    assert mock_upload.call_count == 1
    _, upload_kwargs = mock_upload.call_args
    assert upload_kwargs.get("thread_ts") == "1700000001.000001"


def test_upload_html_report_empty_content(config_with_slack):
    """Returns False immediately when html_content is empty — no upload attempted."""
    ok = upload_html_report(config_with_slack, "", "report.html", thread_ts=None)
    assert ok is False


@patch("scripts.standup.slack_client.upload_file", return_value=False)
@patch("scripts.standup.slack_client.requests.post")
def test_upload_html_report_fallback_to_formicary(mock_post, mock_upload, config_with_slack):
    """Falls back to by-job formicary link when Slack upload fails."""
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": True, "ts": "1700000002.000001"})
    config = {**config_with_slack, "FORMICARY_PUBLIC_URL": "https://formicary.example.com",
              "JOB_ID": "job-1"}
    ok = upload_html_report(config, "<html/>", "report.html", thread_ts="1700000001.0")
    assert ok is True
    assert mock_post.call_count == 1
    fallback_text = mock_post.call_args.kwargs["json"].get("text", "")
    assert "by-job" in fallback_text
    assert "report.html" in fallback_text


@patch("scripts.standup.slack_client.upload_file", return_value=False)
@patch("scripts.standup.slack_client.requests.post")
def test_upload_html_report_fallback_to_by_job_link(mock_post, mock_upload, config_with_slack):
    """Falls back to by-job link when Slack upload fails."""
    mock_post.return_value = MagicMock(ok=True, json=lambda: {"ok": True, "ts": "1700000003.000001"})
    config = {**config_with_slack, "FORMICARY_PUBLIC_URL": "https://formicary.example.com",
              "JOB_ID": "job-2"}
    ok = upload_html_report(config, "<html/>", "report.html",
                            thread_ts="1700000001.0", task_type="run")
    assert ok is True
    fallback_text = mock_post.call_args.kwargs["json"].get("text", "")
    assert "by-job" in fallback_text
    assert "task=run" in fallback_text


# ---------------------------------------------------------------------------
# _pr_queue_to_markdown — table format, badges
# ---------------------------------------------------------------------------

_PR_DATA_SAMPLE = {
    "sprint": "Dev Sprint 3",
    "pr_count": 2,
    "prs": [
        {
            "url": "https://github.com/org/repo/pull/42",
            "jira_key": "PROJ-100",
            "jira_url": "https://org.atlassian.net/browse/PROJ-100",
            "jira_summary": "Fix auth bug",
            "author": "Alice Smith",
            "age_days": 2,
            "approved_by": ["Bob Jones", "Carol Lee"],
            "reviewers": [],
            "ci_status": "success",
            "approval_count": 2,
        },
        {
            "url": "https://github.com/org/repo/pull/10",
            "jira_key": "PROJ-200",
            "jira_summary": "Old work",
            "author": "Dave",
            "age_days": 8,
            "approved_by": [],
            "reviewers": ["Eve"],
            "ci_status": "none",
            "approval_count": 0,
            "priority": "High",
        },
    ],
}


def test_pr_queue_to_markdown_has_overview_table():
    md = _pr_queue_to_markdown(_PR_DATA_SAMPLE, "PR Queue — Dev Sprint 3 — 2026-09-21")
    assert "## Overview" in md
    assert "| Status | Count |" in md
    # All groups appear in overview table
    assert "CI FAILING" in md
    assert "READY TO MERGE" in md


def test_pr_queue_to_markdown_group_section_has_badge():
    md = _pr_queue_to_markdown(_PR_DATA_SAMPLE, "PR Queue")
    # READY TO MERGE group should have ✅ badge in section heading
    assert "✅ READY TO MERGE" in md
    # STALE group for PR with age_days=8
    assert "⚠️ STALE / AT RISK" in md


def test_pr_queue_to_markdown_pr_rows_have_links():
    md = _pr_queue_to_markdown(_PR_DATA_SAMPLE, "PR Queue")
    assert "[PR #42](https://github.com/org/repo/pull/42)" in md
    assert "[PROJ-100](https://org.atlassian.net/browse/PROJ-100)" in md


def test_pr_queue_to_markdown_priority_badge():
    md = _pr_queue_to_markdown(_PR_DATA_SAMPLE, "PR Queue")
    assert "🟠" in md  # High priority badge for PROJ-200


def test_pr_queue_to_markdown_reviewer_badges():
    md = _pr_queue_to_markdown(_PR_DATA_SAMPLE, "PR Queue")
    # Approved PR should show ✅ for approved reviewers
    assert "✅ @Bob" in md
    # Stale PR should show 🔔 for pending reviewers
    assert "🔔 @Eve" in md


def test_pr_queue_to_markdown_empty():
    md = _pr_queue_to_markdown({"prs": [], "sprint": ""}, "PR Queue")
    assert "_No open PRs found._" in md


def test_pr_queue_to_markdown_pipe_escaped():
    """PR titles and Jira summaries with pipe chars must not break the table."""
    data = {
        "prs": [{
            "url": "https://github.com/org/repo/pull/99",
            "jira_summary": "Fix A | B regression",
            "author": "Alice",
            "age_days": 1,
            "approved_by": [],
            "reviewers": [],
            "ci_status": "success",
            "approval_count": 2,
        }],
    }
    md = _pr_queue_to_markdown(data, "PR Queue")
    # Each table row should have exactly 8 pipe separators (9 columns)
    for line in md.splitlines():
        if line.startswith("| ") and "Fix A" in line:
            assert "\\|" in line, "Pipe in title should be escaped"


# ---------------------------------------------------------------------------
# _write_pr_queue_report — writes files only; the post task handles the Slack link
# ---------------------------------------------------------------------------

@patch("scripts.standup.slack_client.requests.post")
def test_write_pr_queue_report_writes_files_no_slack_post(mock_post, tmp_path):
    """_write_pr_queue_report must write report files but NOT post to Slack.
    The completion message with artifact link is handled by the post task."""
    config = {
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL": "dev",
        "FORMICARY_PUBLIC_URL": "https://formicary.example.com",
        "JOB_ID": "job-abc",
    }
    pr_data = {"prs": [], "pr_count": 0}
    _write_pr_queue_report(config, tmp_path, pr_data, "PR Queue", thread_ts=None)
    assert (tmp_path / "reports" / "report.md").exists()
    assert (tmp_path / "reports" / "report.html").exists()
    assert (tmp_path / "reports" / "result.json").exists()
    # No Slack post from this function — post task handles it
    assert not mock_post.called


def test_write_pr_queue_report_writes_result_json(tmp_path):
    """result.json must contain status=DONE and pr_count."""
    import json as _json
    config: dict = {}
    pr_data = {"prs": [], "pr_count": 5}
    _write_pr_queue_report(config, tmp_path, pr_data, "PR Queue", thread_ts=None)
    result = _json.loads((tmp_path / "reports" / "result.json").read_text())
    assert result["status"] == "DONE"
    assert result["pr_count"] == 5


# ---------------------------------------------------------------------------
# slack_client __main__ — single footer with artifact link when report exists
# ---------------------------------------------------------------------------

def _run_slack_client_main(env: dict) -> str:
    """Run slack_client __main__ with the given env and return the posted text."""
    import importlib
    import runpy
    import warnings
    mock_post_ret = MagicMock(ok=True, json=lambda: {"ok": True, "ts": "1.0"})
    with patch("scripts.standup.slack_client.requests.post", return_value=mock_post_ret) as mp, \
         patch.dict("os.environ", env, clear=True), \
         warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        runpy.run_module("scripts.standup.slack_client", run_name="__main__")
    return mp.call_args.kwargs["json"]["text"] if mp.called else ""


def test_slack_client_main_shows_artifact_link_when_report_exists(tmp_path):
    """When reports/report.html exists in workspace, __main__ appends artifact link
    (not 'View job in Formicary') so the post task shows a single merged footer."""
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "report.html").write_text("<html/>")
    posted_text = _run_slack_client_main({
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL": "dev",
        "FORMICARY_PUBLIC_URL": "https://formicary.example.com",
        "JOB_ID": "job-abc",
        "WORKSPACE_DIR": str(tmp_path),
        "MESSAGE": ":white_check_mark: Skill done.",
    })
    assert "View report.html" in posted_text
    assert "task=run" in posted_text
    assert "file=reports/report.html" in posted_text
    assert "View job in Formicary" not in posted_text


def test_slack_client_main_shows_job_link_when_no_report(tmp_path):
    """When no reports/report.html, __main__ appends 'View job in Formicary' (original behaviour)."""
    posted_text = _run_slack_client_main({
        "SLACK_BOT_TOKEN": "xoxb-test",
        "SLACK_CHANNEL": "dev",
        "FORMICARY_PUBLIC_URL": "https://formicary.example.com",
        "JOB_ID": "job-abc",
        "WORKSPACE_DIR": str(tmp_path),
        "MESSAGE": ":white_check_mark: Skill done.",
    })
    assert "View job in Formicary" in posted_text
    assert "View report.html" not in posted_text
