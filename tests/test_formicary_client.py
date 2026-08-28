"""Tests for scripts/slack/formicary_client.py"""

import contextlib
import json
import os as _os
from unittest.mock import MagicMock, patch

import pytest

from scripts.slack.formicary_client import FormicaryClient


@contextlib.contextmanager
def _fake_token(token=""):
    """Suppress ~/.zshrc read so from_env() falls through to the env var."""
    _real_exists = _os.path.exists
    def _no_zshrc(p):
        if str(p).endswith(".zshrc"):
            return False
        return _real_exists(p)
    env = {"FORMICARY_TOKEN": token} if token else {}
    with patch("os.path.exists", side_effect=_no_zshrc), \
         patch.dict("os.environ", env, clear=False):
        yield


@pytest.fixture
def client():
    return FormicaryClient(base_url="http://formicary:7777", token="test-token")


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------

def test_from_env_defaults(monkeypatch):
    monkeypatch.delenv("FORMICARY_URL", raising=False)
    monkeypatch.delenv("FORMICARY_TOKEN", raising=False)
    with _fake_token():
        c = FormicaryClient.from_env()
    assert c.base_url == "http://localhost:7777"
    assert c.token == ""


def test_from_env_reads_env_vars(monkeypatch):
    monkeypatch.setenv("FORMICARY_URL", "http://custom:9999")
    with _fake_token("my-token"):
        c = FormicaryClient.from_env()
    assert c.base_url == "http://custom:9999"
    assert c.token == "my-token"


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------

@patch("scripts.slack.formicary_client.requests.post")
def test_submit_sends_correct_payload(mock_post, client):
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"id": "job-42", "job_type": "ai-gh-review"}
    mock_post.return_value = mock_resp

    result = client.submit("ai-gh-review", {"PRUrl": "https://github.com/x/y/pull/1", "SlackChannel": "C123"})

    assert result["id"] == "job-42"
    call_kwargs = mock_post.call_args.kwargs
    payload = call_kwargs["json"]
    assert payload["job_type"] == "ai-gh-review"
    assert payload["params"]["PRUrl"] == "https://github.com/x/y/pull/1"
    assert call_kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert "user_key" not in payload  # not set when not passed


@patch("scripts.slack.formicary_client.requests.post")
def test_submit_sets_user_key(mock_post, client):
    """user_key is included in payload when provided."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"id": "job-99", "job_type": "ai-standup-jira"}
    mock_post.return_value = mock_resp

    result = client.submit("ai-standup-jira", {"SlackThreadTs": "1234.567"}, user_key="ai-standup-jira:1234.567")

    assert result["id"] == "job-99"
    payload = mock_post.call_args.kwargs["json"]
    assert payload["user_key"] == "ai-standup-jira:1234.567"


@patch("scripts.slack.formicary_client.requests.post")
def test_submit_returns_existing_on_409(mock_post, client):
    """409 Conflict (fixed server): existing job returned in response body."""
    existing = {"id": "existing-job", "job_type": "ai-standup-jira"}
    mock_post.return_value = MagicMock(
        ok=False, status_code=409, text="conflict",
        json=MagicMock(return_value=existing),
    )

    result = client.submit(
        "ai-standup-jira",
        {"SlackThreadTs": "1234.567"},
        user_key="ai-standup-jira:1234.567",
    )
    assert result["id"] == "existing-job"


@patch("scripts.slack.formicary_client.requests.get")
@patch("scripts.slack.formicary_client.requests.post")
def test_submit_returns_existing_on_500_unique(mock_post, mock_get, client):
    """500 with UNIQUE text (unfixed server): falls back to find_jobs by SlackThreadTs."""
    mock_post.return_value = MagicMock(
        ok=False, status_code=500,
        text='{"error":"UNIQUE constraint failed: formicary_job_requests.user_key"}',
    )
    mock_get.return_value = MagicMock(ok=True, json=MagicMock(return_value=[
        {"id": "existing-job", "job_type": "ai-standup-jira",
         "params": [{"name": "SlackThreadTs", "value": "1234.567"}]}
    ]))

    result = client.submit(
        "ai-standup-jira",
        {"SlackThreadTs": "1234.567"},
        user_key="ai-standup-jira:1234.567",
    )
    assert result["id"] == "existing-job"


@patch("scripts.slack.formicary_client.requests.post")
def test_submit_returns_empty_on_http_error(mock_post, client):
    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 401
    mock_resp.text = "Unauthorized"
    mock_post.return_value = mock_resp

    result = client.submit("ai-gh-review", {})
    assert result == {}


@patch("scripts.slack.formicary_client.requests.post")
def test_submit_returns_empty_on_exception(mock_post, client):
    mock_post.side_effect = ConnectionError("refused")
    result = client.submit("ai-gh-review", {})
    assert result == {}


# ---------------------------------------------------------------------------
# find_jobs
# ---------------------------------------------------------------------------

@patch("scripts.slack.formicary_client.requests.get")
def test_find_jobs_filters_by_state(mock_get, client):
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {
        "records": [
            {"id": "j1", "params": {"SlackThreadTs": "111.000"}},
            {"id": "j2", "params": {"SlackThreadTs": "222.000"}},
        ]
    }
    mock_get.return_value = mock_resp

    jobs = client.find_jobs(state="PAUSED", var_filter={"SlackThreadTs": "111.000"})
    assert len(jobs) == 1
    assert jobs[0]["id"] == "j1"

    call_params = mock_get.call_args.kwargs["params"]
    assert call_params["job_state"] == "PAUSED"
    assert "pageSize" in call_params


@patch("scripts.slack.formicary_client.requests.get")
def test_find_jobs_handles_list_response(mock_get, client):
    """API may return a plain list, not wrapped in 'records'."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = [{"id": "j1", "params": {"X": "Y"}}]
    mock_get.return_value = mock_resp

    jobs = client.find_jobs()
    assert len(jobs) == 1


@patch("scripts.slack.formicary_client.requests.get")
def test_find_jobs_array_params_var_filter(mock_get, client):
    """var_filter works when API returns params as [{name, value}] array."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = {"records": [
        {"id": "j1", "job_type": "ai-standup-jira",
         "params": [{"name": "SlackThreadTs", "value": "999.111"}, {"name": "SlackChannel", "value": "C123"}]},
        {"id": "j2", "job_type": "ai-standup-jira",
         "params": [{"name": "SlackThreadTs", "value": "000.000"}, {"name": "SlackChannel", "value": "C123"}]},
    ]}
    mock_get.return_value = mock_resp

    jobs = client.find_jobs(var_filter={"SlackThreadTs": "999.111"})
    assert len(jobs) == 1
    assert jobs[0]["id"] == "j1"


@patch("scripts.slack.formicary_client.requests.get")
def test_find_jobs_state_any_omits_state_param(mock_get, client):
    """state='ANY' sends no job_state query param."""
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.json.return_value = []
    mock_get.return_value = mock_resp

    client.find_jobs(state="ANY")
    call_params = mock_get.call_args.kwargs["params"]
    assert "job_state" not in call_params


@patch("scripts.slack.formicary_client.requests.get")
def test_find_jobs_returns_empty_on_error(mock_get, client):
    mock_resp = MagicMock()
    mock_resp.ok = False
    mock_resp.status_code = 500
    mock_resp.text = "server error"
    mock_get.return_value = mock_resp

    jobs = client.find_jobs()
    assert jobs == []


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------

@patch("scripts.slack.formicary_client.requests.post")
def test_resume_with_variables_sends_params_in_body(mock_post, client):
    """resume(variables=...) sends params in POST /trigger body — single request."""
    mock_post.return_value = MagicMock(ok=True)

    ok = client.resume("job-paused", variables={"ReplyText": "looks good"})
    assert ok is True

    mock_post.assert_called_once()
    url = mock_post.call_args[0][0]
    assert "job-paused" in url
    assert "trigger" in url
    body = mock_post.call_args.kwargs["json"]
    assert body["params"]["ReplyText"] == "looks good"


@patch("scripts.slack.formicary_client.requests.post")
def test_resume_without_variables_just_triggers(mock_post, client):
    """resume(variables=None) just POSTs to trigger with no body."""
    mock_post.return_value = MagicMock(ok=True)

    ok = client.resume("job-42")
    assert ok is True
    mock_post.assert_called_once()
    assert "trigger" in mock_post.call_args[0][0]
    body = mock_post.call_args.kwargs.get("json")
    assert body is None


@patch("scripts.slack.formicary_client.requests.post")
def test_resume_returns_false_when_trigger_fails(mock_post, client):
    mock_post.return_value = MagicMock(ok=False, status_code=500, text="error")

    ok = client.resume("job-missing", variables={"Decision": "approve"})
    assert ok is False


# ---------------------------------------------------------------------------
# trigger_pending_or_submit
# ---------------------------------------------------------------------------

@patch("scripts.slack.formicary_client.requests.post")
@patch("scripts.slack.formicary_client.requests.get")
def test_trigger_pending_injects_params_in_post_body(mock_get, mock_post, client):
    """When a PENDING job exists, params are sent in the POST /trigger body — no GET/PUT needed."""
    mock_get.return_value = MagicMock(ok=True, json=MagicMock(return_value=[
        {"id": "pending-job", "job_type": "ai-standup-jira", "cron_triggered": True, "params": {"SlackChannel": "old-ch"}}
    ]))
    mock_post.return_value = MagicMock(ok=True, json=MagicMock(return_value={}))

    result = client.trigger_pending_or_submit("ai-standup-jira", {
        "SlackChannel": "sb-test", "SlackThreadTs": "999.111"
    })

    assert result["id"] == "pending-job"
    # One POST to the trigger endpoint carrying the params in the body
    assert mock_post.call_count == 1
    trigger_url = mock_post.call_args[0][0]
    assert "trigger" in trigger_url
    body = mock_post.call_args.kwargs["json"]
    assert body["params"]["SlackThreadTs"] == "999.111"
    assert body["params"]["SlackChannel"] == "sb-test"


@patch("scripts.slack.formicary_client.requests.post")
@patch("scripts.slack.formicary_client.requests.get")
def test_trigger_pending_no_params_sends_empty_body(mock_get, mock_post, client):
    """When a PENDING job exists but no params provided, POST /trigger with empty body."""
    mock_get.return_value = MagicMock(ok=True, json=MagicMock(return_value=[
        {"id": "pending-job", "job_type": "ai-standup-jira", "cron_triggered": True}
    ]))
    mock_post.return_value = MagicMock(ok=True, json=MagicMock(return_value={}))

    result = client.trigger_pending_or_submit("ai-standup-jira", {})

    assert result["id"] == "pending-job"
    assert mock_post.call_count == 1
    # No params — body should be None (no JSON body)
    body = mock_post.call_args.kwargs.get("json")
    assert body is None


@patch("scripts.slack.formicary_client.requests.post")
@patch("scripts.slack.formicary_client.requests.get")
def test_trigger_pending_uses_cancelled_slot_as_fallback(mock_get, mock_post, client):
    """When no WAITING slot exists but a CANCELLED cron slot does, trigger that instead.

    The Go Trigger() method now accepts CANCELLED cron slots and re-activates them
    (sets state=PENDING, rotates user_key). This lets operators recover a broken
    cron schedule without direct DB access.
    """
    # First GET (WAITING) → empty; second GET (CANCELLED) → one cron-triggered record
    empty_resp = MagicMock(ok=True, json=MagicMock(return_value=[]))
    cancelled_resp = MagicMock(ok=True, json=MagicMock(return_value=[
        {"id": "cancelled-slot", "job_type": "ai-standup-jira", "cron_triggered": True}
    ]))
    mock_get.side_effect = [empty_resp, cancelled_resp]
    mock_post.return_value = MagicMock(ok=True)

    result = client.trigger_pending_or_submit("ai-standup-jira", {"SlackThreadTs": "1.1"})
    assert result.get("id") == "cancelled-slot"
    # Must NOT attempt a submit POST — only a trigger POST
    mock_post.assert_called_once()
    trigger_url = mock_post.call_args.args[0] if mock_post.call_args.args else mock_post.call_args[0][0]
    assert "cancelled-slot/trigger" in trigger_url


@patch("scripts.slack.formicary_client.requests.post")
@patch("scripts.slack.formicary_client.requests.get")
def test_trigger_pending_returns_no_cron_slot_when_nothing_found(mock_get, mock_post, client):
    """When neither WAITING nor CANCELLED records exist, return _no_cron_slot sentinel.

    Falling back to submit() is wrong: Formicary auto-assigns a deterministic user_key
    for the next cron slot, so submit() always hits a UNIQUE constraint when the previous
    CANCELLED record still owns that key.
    """
    # Both GET calls (WAITING and CANCELLED) return empty
    mock_get.return_value = MagicMock(ok=True, json=MagicMock(return_value=[]))

    result = client.trigger_pending_or_submit("ai-standup-jira", {"SlackThreadTs": "1.1"})
    assert result == {"_no_cron_slot": True}
    # Must NOT attempt a submit POST
    mock_post.assert_not_called()


@patch("scripts.slack.formicary_client.requests.post")
@patch("scripts.slack.formicary_client.requests.get")
def test_trigger_pending_returns_empty_on_trigger_error(mock_get, mock_post, client):
    """Returns {} when the trigger POST fails."""
    mock_get.return_value = MagicMock(ok=True, json=MagicMock(return_value=[
        {"id": "pending-job", "job_type": "ai-standup-jira", "cron_triggered": True}
    ]))
    mock_post.return_value = MagicMock(ok=False, status_code=500, text="internal error")

    result = client.trigger_pending_or_submit("ai-standup-jira", {"SlackThreadTs": "bad"})
    assert result == {}
