"""Tests for scripts/slack/router.py — dispatch logic, thread reply, block actions."""

import json
from unittest.mock import MagicMock, patch

import pytest

from scripts.slack.registry import Registry
from scripts.slack.router import (
    _dispatch,
    handle_new_request,
    handle_thread_reply,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry(tmp_path):
    """A real registry loaded from minimal fixture YAML files."""
    wf_yaml = tmp_path / "workflows.yml"
    wf_yaml.write_text("""
workflows:
  - name: gh-review
    job_type: ai-gh-review
    shape: ai-review
    triggers: ["review", "code review", "pr review"]
    skill: ygs-review-pr
    id_var: PRUrl
    required_vars: [PRUrl]
    target_kind: github
    description: "Review a GitHub PR"
  - name: standup
    job_type: ai-adhoc
    shape: ai-adhoc
    triggers: ["standup", "status"]
    skill: ygs-standup
    id_var: Prompt
    required_vars: []
    target_kind: any
    description: "Run standup"
""")
    sk_yaml = tmp_path / "skills.yml"
    sk_yaml.write_text("""
skills:
  - name: ygs-review-pr
    source: github.com/bhatti/you-got-skills
    path: skills/ygs-review-pr
    ref: main
    description: PR review skill
""")
    return Registry(wf_yaml, sk_yaml)


@pytest.fixture
def mock_formicary():
    """A mocked FormicaryClient."""
    client = MagicMock()
    client.submit.return_value = {"id": "job-123", "job_type": "ai-gh-review"}
    client.find_jobs.return_value = []
    client.resume.return_value = True
    return client


@pytest.fixture
def config():
    return {
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-test",
        "SLACK_BOT_TOKEN": "xoxb-test",
    }


# ---------------------------------------------------------------------------
# handle_new_request
# ---------------------------------------------------------------------------

def test_handle_new_request_slash_review(registry, mock_formicary, config):
    """'/review https://github.com/x/y/pull/1' submits ai-gh-review."""
    say = MagicMock()

    with patch("scripts.slack.router._get_registry", return_value=registry), \
         patch("scripts.slack.router._get_client", return_value=mock_formicary):
        handle_new_request(
            text="review https://github.com/org/repo/pull/42",
            channel="C123",
            ts="111.000",
            say=say,
            config=config,
        )

    mock_formicary.submit.assert_called_once()
    call_kwargs = mock_formicary.submit.call_args
    job_type = call_kwargs[0][0]
    params = call_kwargs[0][1]
    assert job_type == "ai-gh-review"
    assert "PRUrl" in params
    assert params["SlackChannel"] == "C123"
    assert params["SlackThreadTs"] == "111.000"
    say.assert_called_once()
    assert "job-123" in say.call_args.kwargs.get("text", "")


def test_handle_new_request_unknown_intent_replies(registry, mock_formicary, config):
    """Unknown intent replies that no workflow found (no submit)."""
    say = MagicMock()

    with patch("scripts.slack.router._get_registry", return_value=registry), \
         patch("scripts.slack.router._get_client", return_value=mock_formicary), \
         patch("scripts.slack.router.classify_intent", return_value=("unknown", "any", "")):
        handle_new_request(
            text="deploy the thing to prod",
            channel="C123",
            ts="111.000",
            say=say,
            config=config,
        )

    mock_formicary.submit.assert_not_called()
    say.assert_called_once()
    assert "don't have" in say.call_args.kwargs.get("text", "").lower()


def test_handle_new_request_missing_required_var(registry, mock_formicary, config):
    """Review without a PR URL reports missing required vars."""
    say = MagicMock()

    with patch("scripts.slack.router._get_registry", return_value=registry), \
         patch("scripts.slack.router._get_client", return_value=mock_formicary), \
         patch("scripts.slack.router.classify_intent", return_value=("review", "github", "")):
        handle_new_request(
            text="review",
            channel="C123",
            ts="111.000",
            say=say,
            config=config,
        )

    mock_formicary.submit.assert_not_called()
    say.assert_called_once()
    msg = say.call_args.kwargs.get("text", "")
    assert "PRUrl" in msg or "need" in msg.lower()


def test_handle_new_request_standup_no_required_vars(registry, mock_formicary, config):
    """Standup has no required vars — submits immediately."""
    say = MagicMock()

    with patch("scripts.slack.router._get_registry", return_value=registry), \
         patch("scripts.slack.router._get_client", return_value=mock_formicary):
        mock_formicary.submit.return_value = {"id": "job-standup"}
        handle_new_request(
            text="standup",
            channel="C999",
            ts="222.000",
            say=say,
            config=config,
        )

    mock_formicary.submit.assert_called_once()
    call_job_type = mock_formicary.submit.call_args[0][0]
    assert call_job_type == "ai-adhoc"


# ---------------------------------------------------------------------------
# handle_thread_reply
# ---------------------------------------------------------------------------

def test_handle_thread_reply_resumes_paused_job(registry, mock_formicary, config):
    """Thread reply to a paused job calls resume with ReplyText."""
    say = MagicMock()
    paused_job = {"id": "job-paused", "params": {"SlackThreadTs": "555.000"}}
    mock_formicary.find_jobs.return_value = [paused_job]

    event = {"thread_ts": "555.000", "ts": "556.000", "text": "looks good, please proceed", "channel": "C1"}

    with patch("scripts.slack.router._get_registry", return_value=registry), \
         patch("scripts.slack.router._get_client", return_value=mock_formicary):
        handle_thread_reply(event, say, config)

    mock_formicary.find_jobs.assert_called_once_with(state="PAUSED", var_filter={"SlackThreadTs": "555.000"})
    mock_formicary.resume.assert_called_once_with("job-paused", variables={"ReplyText": "looks good, please proceed"})
    say.assert_called_once()
    assert "resuming" in say.call_args.kwargs.get("text", "").lower()


def test_handle_thread_reply_falls_through_when_no_paused_job(registry, mock_formicary, config):
    """No paused job on thread → treat as new request."""
    say = MagicMock()
    mock_formicary.find_jobs.return_value = []
    mock_formicary.submit.return_value = {"id": "job-new"}

    event = {"thread_ts": "500.000", "ts": "501.000", "text": "standup", "channel": "C2"}

    with patch("scripts.slack.router._get_registry", return_value=registry), \
         patch("scripts.slack.router._get_client", return_value=mock_formicary):
        handle_thread_reply(event, say, config)

    # Should submit since standup has no required vars
    mock_formicary.submit.assert_called_once()


# ---------------------------------------------------------------------------
# _dispatch — bot mention stripping
# ---------------------------------------------------------------------------

def test_dispatch_strips_bot_mention(registry, mock_formicary, config):
    """<@U123> prefix is stripped before routing."""
    say = MagicMock()
    mock_formicary.submit.return_value = {"id": "job-x"}

    event = {"ts": "100.000", "text": "<@U123ABC> standup", "channel": "C1"}

    with patch("scripts.slack.router._get_registry", return_value=registry), \
         patch("scripts.slack.router._get_client", return_value=mock_formicary):
        _dispatch(event, say, config)

    mock_formicary.submit.assert_called_once()
    params = mock_formicary.submit.call_args[0][1]
    # Skill should be ygs-standup not contain the mention prefix
    assert params.get("Skill") == "ygs-standup"
