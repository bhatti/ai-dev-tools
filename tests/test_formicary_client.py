"""Tests for scripts/slack/formicary_client.py"""

import json
from unittest.mock import MagicMock, patch

import pytest

from scripts.slack.formicary_client import FormicaryClient


@pytest.fixture
def client():
    return FormicaryClient(base_url="http://formicary:7777", token="test-token")


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------

def test_from_env_defaults(monkeypatch):
    monkeypatch.delenv("FORMICARY_URL", raising=False)
    monkeypatch.delenv("FORMICARY_TOKEN", raising=False)
    c = FormicaryClient.from_env()
    assert c.base_url == "http://localhost:7777"
    assert c.token == ""


def test_from_env_reads_env_vars(monkeypatch):
    monkeypatch.setenv("FORMICARY_URL", "http://custom:9999")
    monkeypatch.setenv("FORMICARY_TOKEN", "my-token")
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
    assert call_params["state"] == "PAUSED"


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
@patch("scripts.slack.formicary_client.requests.put")
@patch("scripts.slack.formicary_client.requests.get")
def test_resume_with_variables_four_step_flow(mock_get, mock_put, mock_post, client):
    """resume(variables=...) does GET→merge→PUT→trigger."""
    mock_get.return_value = MagicMock(ok=True, json=MagicMock(return_value={
        "id": "job-paused", "params": {"SlackThreadTs": "555.000"}
    }))
    mock_put.return_value = MagicMock(ok=True, json=MagicMock(return_value={}))
    mock_post.return_value = MagicMock(ok=True, json=MagicMock(return_value={}))

    ok = client.resume("job-paused", variables={"ReplyText": "looks good"})
    assert ok is True

    # GET called for current job
    mock_get.assert_called_once()
    assert "job-paused" in mock_get.call_args[0][0]

    # PUT called with merged params
    mock_put.assert_called_once()
    put_payload = mock_put.call_args.kwargs["json"]
    assert put_payload["params"]["ReplyText"] == "looks good"
    assert put_payload["params"]["SlackThreadTs"] == "555.000"

    # POST trigger called
    mock_post.assert_called_once()
    assert "trigger" in mock_post.call_args[0][0]


@patch("scripts.slack.formicary_client.requests.post")
def test_resume_without_variables_just_triggers(mock_post, client):
    """resume(variables=None) skips GET+PUT and just triggers."""
    mock_post.return_value = MagicMock(ok=True)

    ok = client.resume("job-42")
    assert ok is True
    mock_post.assert_called_once()
    assert "trigger" in mock_post.call_args[0][0]


@patch("scripts.slack.formicary_client.requests.post")
@patch("scripts.slack.formicary_client.requests.put")
@patch("scripts.slack.formicary_client.requests.get")
def test_resume_returns_false_when_get_fails(mock_get, mock_put, mock_post, client):
    mock_get.return_value = MagicMock(ok=False, status_code=404, text="not found")

    ok = client.resume("job-missing", variables={"Decision": "approve"})
    assert ok is False
    mock_put.assert_not_called()
    mock_post.assert_not_called()
