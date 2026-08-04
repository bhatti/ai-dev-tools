"""Tests for scripts/slack/router.py — dispatch logic, thread reply, block actions."""

import json
from unittest.mock import MagicMock, patch

import pytest

from scripts.slack.registry import Registry
from scripts.slack.router import (
    _dispatch,
    _normalize_slack_text,
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
    client.trigger_pending_or_submit.return_value = {"id": "job-standup-new", "job_type": "ai-standup-jira"}
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
    """Unknown intent replies that no workflow found and hints at @bot help."""
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
    msg = say.call_args.kwargs.get("text", "").lower()
    assert "don't have" in msg
    assert "help" in msg


def test_handle_new_request_help_verb(registry, mock_formicary, config):
    """'help' verb returns the help message without submitting any job."""
    say = MagicMock()

    with patch("scripts.slack.router._get_registry", return_value=registry), \
         patch("scripts.slack.router._get_client", return_value=mock_formicary):
        handle_new_request(
            text="help",
            channel="C123",
            ts="111.000",
            say=say,
            config=config,
        )

    mock_formicary.submit.assert_not_called()
    say.assert_called_once()
    msg = say.call_args.kwargs.get("text", "")
    assert "Available commands" in msg
    assert "Adding a new skill" in msg


def test_handle_new_request_help_uses_configured_bot_name(registry, mock_formicary, config):
    """Help message uses SLACK_BOT_NAME from config, not hardcoded '@bot'."""
    say = MagicMock()
    cfg = {**config, "SLACK_BOT_NAME": "@myrobot"}

    with patch("scripts.slack.router._get_registry", return_value=registry), \
         patch("scripts.slack.router._get_client", return_value=mock_formicary):
        handle_new_request(
            text="help",
            channel="C123",
            ts="111.000",
            say=say,
            config=cfg,
        )

    msg = say.call_args.kwargs.get("text", "")
    assert "@myrobot" in msg
    assert "@bot" not in msg


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
    """Standup has no required vars — submits immediately (fixture registry has cron=false)."""
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


def test_handle_new_request_cron_standup_calls_trigger_pending(tmp_path, mock_formicary, config):
    """Cron standup (entry.cron=True) calls trigger_pending_or_submit, not submit."""
    wf_yaml = tmp_path / "workflows.yml"
    wf_yaml.write_text("""
workflows:
  - name: standup-cron
    job_type: ai-standup-jira
    shape: ai-standup
    triggers: ["standup"]
    skill: ""
    id_var: ""
    required_vars: []
    target_kind: any
    cron: true
    description: "Cron standup"
""")
    sk_yaml = tmp_path / "skills.yml"
    sk_yaml.write_text("skills: []\n")
    cron_registry = Registry(wf_yaml, sk_yaml)

    say = MagicMock()
    with patch("scripts.slack.router._get_registry", return_value=cron_registry), \
         patch("scripts.slack.router._get_client", return_value=mock_formicary):
        handle_new_request(
            text="standup",
            channel="C999",
            ts="333.000",
            say=say,
            config=config,
        )

    mock_formicary.trigger_pending_or_submit.assert_called_once()
    job_type, params = mock_formicary.trigger_pending_or_submit.call_args[0]
    assert job_type == "ai-standup-jira"
    assert params["SlackThreadTs"] == "333.000"
    # Regular submit should NOT be called for cron entries
    mock_formicary.submit.assert_not_called()
    say.assert_called_once()


def test_handle_new_request_cron_no_slot_gives_friendly_error(tmp_path, mock_formicary, config):
    """When trigger_pending_or_submit returns _no_cron_slot, post a human-readable message."""
    wf_yaml = tmp_path / "workflows.yml"
    wf_yaml.write_text("""
workflows:
  - name: standup-cron
    job_type: ai-standup-jira
    shape: ai-standup
    triggers: ["standup"]
    skill: ""
    id_var: ""
    required_vars: []
    target_kind: any
    cron: true
    description: "Cron standup"
""")
    sk_yaml = tmp_path / "skills.yml"
    sk_yaml.write_text("skills: []\n")
    cron_registry = Registry(wf_yaml, sk_yaml)

    mock_formicary.trigger_pending_or_submit.return_value = {"_no_cron_slot": True}

    say = MagicMock()
    with patch("scripts.slack.router._get_registry", return_value=cron_registry), \
         patch("scripts.slack.router._get_client", return_value=mock_formicary):
        handle_new_request(
            text="standup",
            channel="C999",
            ts="444.000",
            say=say,
            config=config,
        )

    say.assert_called_once()
    msg = say.call_args.kwargs.get("text", "")
    assert "no scheduled slot" in msg.lower() or "disable" in msg.lower() or "re-enable" in msg.lower()


def test_handle_new_request_default_tracker_passed_in_params(tmp_path, mock_formicary, config):
    """DEFAULT_TRACKER from config is forwarded as DefaultTracker job param."""
    wf_yaml = tmp_path / "workflows.yml"
    wf_yaml.write_text("""
workflows:
  - name: pr-queue
    job_type: ai-adhoc
    shape: ai-adhoc
    triggers: ["prs", "pr queue"]
    skill: ygs-pr-queue
    id_var: ""
    required_vars: []
    target_kind: any
    description: "List open PRs"
""")
    sk_yaml = tmp_path / "skills.yml"
    sk_yaml.write_text("skills: []\n")
    pr_registry = Registry(wf_yaml, sk_yaml)

    say = MagicMock()
    cfg = {**config, "DEFAULT_TRACKER": "jira"}
    with patch("scripts.slack.router._get_registry", return_value=pr_registry), \
         patch("scripts.slack.router._get_client", return_value=mock_formicary):
        handle_new_request(
            text="prs",
            channel="C111",
            ts="555.000",
            say=say,
            config=cfg,
        )

    mock_formicary.submit.assert_called_once()
    _, params = mock_formicary.submit.call_args[0]
    assert params.get("DefaultTracker") == "jira"


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
# _normalize_slack_text — Slack mrkdwn link stripping
# ---------------------------------------------------------------------------

def test_normalize_slack_text_jira_link_extracts_key():
    """<url|title> for a Jira browse URL is replaced with just the issue key."""
    raw = "implement <https://company.atlassian.net/browse/PROJ-123|Some issue title here>"
    assert _normalize_slack_text(raw) == "implement PROJ-123"


def test_normalize_slack_text_jira_link_with_parens_in_title():
    """Jira titles with parentheses don't cause shell syntax errors after normalization."""
    raw = "<https://taktak.atlassian.net/browse/CRIBL-40452|Consider how WorkerHolder.reportMetrics() might be made faster>"
    assert _normalize_slack_text(raw) == "CRIBL-40452"


def test_normalize_slack_text_github_pr_url_kept():
    """GitHub PR <url|text> links are replaced with the bare URL (not stripped)."""
    raw = "review <https://github.com/org/repo/pull/42|Fix auth bug>"
    result = _normalize_slack_text(raw)
    assert "github.com/org/repo/pull/42" in result
    assert "Fix auth bug" not in result


def test_normalize_slack_text_bare_url():
    """Bare <url> without display text is unwrapped to the URL."""
    raw = "review <https://github.com/org/repo/pull/99>"
    assert _normalize_slack_text(raw) == "review https://github.com/org/repo/pull/99"


def test_normalize_slack_text_plain_text_unchanged():
    """Text without Slack links is returned unchanged."""
    assert _normalize_slack_text("standup") == "standup"
    assert _normalize_slack_text("implement PROJ-42") == "implement PROJ-42"


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


def test_dispatch_normalises_jira_slack_link(mock_formicary, config, tmp_path):
    """Slack auto-linked Jira URLs like <url|title> are reduced to the issue key."""
    wf_yaml = tmp_path / "workflows.yml"
    wf_yaml.write_text("""
workflows:
  - name: jira-implement
    job_type: ai-jira-implement
    shape: ai-implement
    triggers: ["implement", "build"]
    skill: ygs-implement
    id_var: IssueNumber
    required_vars: [IssueNumber]
    target_kind: jira
    description: "Implement a Jira issue"
""")
    sk_yaml = tmp_path / "skills.yml"
    sk_yaml.write_text("skills: []\n")
    from scripts.slack.registry import Registry
    reg = Registry(wf_yaml, sk_yaml)

    say = MagicMock()
    mock_formicary.submit.return_value = {"id": "job-impl"}

    # Slack-formatted link: <url|issue title with parens>
    slack_text = "<@U999> implement <https://company.atlassian.net/browse/PROJ-42|Fix WorkerHolder.doSomething() race>"
    event = {"ts": "200.000", "text": slack_text, "channel": "C2"}

    with patch("scripts.slack.router._get_registry", return_value=reg), \
         patch("scripts.slack.router._get_client", return_value=mock_formicary):
        _dispatch(event, say, config)

    mock_formicary.submit.assert_called_once()
    _, params = mock_formicary.submit.call_args[0]
    assert params.get("IssueNumber") == "PROJ-42"
