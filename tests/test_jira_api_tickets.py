"""Tests for the explicit-error Jira calls in scripts/common/jira_api.py, against the stateful fake Jira server."""

from __future__ import annotations

import pytest
import requests

from scripts.common import adf, jira_api
from scripts.common.jira_api import JiraError
from tests.fakes.jira import FakeJira


@pytest.fixture()
def jira(monkeypatch):
    jira_api._field_id_cache.clear()
    with FakeJira(monkeypatch) as fake:
        yield fake


@pytest.fixture()
def cfg(jira):
    return {"JIRA_BASE_URL": jira.base, "JIRA_EMAIL": "tester@example.com", "JIRA_API_TOKEN": "test-token-123"}


def test_search_all_follows_every_page(jira, cfg):
    for i in range(1, 8):
        jira.add_issue(f"PROJ-{i}", f"t{i}")
    jira.page_size = 3
    issues = jira_api.search_issues_all(cfg, "project = PROJ", ["summary"])
    assert [i["key"] for i in issues] == [f"PROJ-{i}" for i in range(1, 8)]
    assert sum(1 for m, p, _ in jira.requests if p.endswith("/search/jql")) == 3


def test_search_all_stops_at_the_limit(jira, cfg):
    for i in range(1, 8):
        jira.add_issue(f"PROJ-{i}", f"t{i}")
    jira.page_size = 3
    assert len(jira_api.search_issues_all(cfg, "project = PROJ", ["summary"], limit=4)) == 4


def test_a_rejected_search_raises_instead_of_returning_nothing(jira, cfg):
    jira.fail("/search/jql", 400)
    with pytest.raises(JiraError) as e:
        jira_api.search_issues_all(cfg, "bad jql", ["summary"])
    assert e.value.status == 400


def test_rate_limits_are_retried_after_retry_after(jira, cfg):
    jira.add_issue("PROJ-1", "t")
    jira.fail("/search/jql", 429, times=2, headers={"Retry-After": "0"})
    assert [i["key"] for i in jira_api.search_issues_all(cfg, "x", ["summary"])] == ["PROJ-1"]
    assert sum(1 for m, p, _ in jira.requests if p.endswith("/search/jql")) == 3


def test_a_persistent_server_error_surfaces_after_the_retries(jira, cfg):
    jira.fail("/search/jql", 503, times=10, headers={"Retry-After": "0"})
    with pytest.raises(JiraError):
        jira_api.search_issues_all(cfg, "x", ["summary"])


def test_fetch_issue_returns_requested_fields_and_raises_when_missing(jira, cfg):
    jira.add_issue("PROJ-1", "title", points=5)
    issue = jira_api.fetch_issue(cfg, "PROJ-1", ["summary"])
    assert issue["fields"] == {"summary": "title"}
    with pytest.raises(JiraError) as e:
        jira_api.fetch_issue(cfg, "PROJ-9")
    assert e.value.status == 404


def test_list_fields_exposes_ids_and_names(jira, cfg):
    assert {"id": "customfield_10016", "name": "Story point estimate"} in jira_api.list_fields(cfg)


def test_set_description_replaces_the_stored_document(jira, cfg):
    jira.add_issue("PROJ-1", "t", description="## Why\nbecause")
    doc = adf.append_section(jira.issues["PROJ-1"]["fields"]["description"], "Plan (generated)", adf.from_markdown("## Plan (generated)\n- x"))
    jira_api.set_description(cfg, "PROJ-1", doc)
    assert "Plan (generated)" in jira.description_text("PROJ-1") and "because" in jira.description_text("PROJ-1")


def test_credentials_are_never_part_of_an_error_message(jira, cfg):
    jira.fail("/rest/api/3/field", 401)
    with pytest.raises(JiraError) as e:
        jira_api.list_fields(cfg)
    assert "test-token-123" not in str(e.value)


def test_a_non_json_success_response_is_an_explicit_error(jira, cfg, monkeypatch):
    """A proxy or login page answering 200 must not crash the caller with a JSON decode error."""

    class Page:
        status_code, ok, text, headers = 200, True, "<html>sign in</html>", {}

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(requests, "request", lambda *a, **k: Page())
    with pytest.raises(JiraError, match="not JSON"):
        jira_api.search_issues_all(cfg, "x", ["summary"])
    with pytest.raises(JiraError, match="not JSON"):
        jira_api.list_fields(cfg)


def test_list_comments_follows_every_page(jira, cfg):
    jira.add_issue("PROJ-1", "t", comments=[f"c{i}" for i in range(7)])
    jira.comment_page_size = 3
    comments = jira_api.list_comments(cfg, "PROJ-1")
    assert [adf.to_text(c["body"]).strip() for c in comments] == [f"c{i}" for i in range(7)]


def test_an_empty_page_with_a_token_cannot_loop_forever(jira, cfg, monkeypatch):
    calls = []

    def fake_call(config, method, path, **kw):
        calls.append(path)

        class R:
            status_code, ok = 200, True

            def json(self):
                return {"issues": [], "nextPageToken": "again"}
        return R()
    monkeypatch.setattr(jira_api, "_call", fake_call)
    assert jira_api.search_issues_all(cfg, "x", ["summary"]) == [] and len(calls) == 1


def test_a_comment_keeps_its_structure_and_is_retried_and_redacted(jira, cfg):
    jira.add_issue("PROJ-1", "t")
    jira.fail("/comment", 429, times=1, headers={"Retry-After": "0"}, method="POST")
    jira_api.add_comment_markdown(cfg, "PROJ-1", "## Plan\n- one\n- two with **bold**")
    body = jira.issues["PROJ-1"]["fields"]["comment"]["comments"][-1]["body"]
    assert [n["type"] for n in body["content"]] == ["heading", "bulletList"]
    jira.fail("/comment", 400, method="POST")
    with pytest.raises(JiraError) as e:
        jira_api.add_comment_markdown(cfg, "PROJ-1", "x")
    assert "test-token-123" not in str(e.value)


def test_a_comment_is_not_retried_after_a_server_error_because_it_may_have_been_applied(jira, cfg):
    jira.add_issue("PROJ-1", "t")
    jira.fail("/comment", 500, times=1, headers={"Retry-After": "0"}, method="POST")
    with pytest.raises(JiraError):
        jira_api.add_comment_markdown(cfg, "PROJ-1", "note")
    assert sum(1 for m, p, _ in jira.requests if m == "POST" and p.endswith("/comment")) == 1
    jira.fail("/comment", 429, times=1, headers={"Retry-After": "0"}, method="POST")
    jira_api.add_comment_markdown(cfg, "PROJ-1", "note")
    assert len(jira.issues["PROJ-1"]["fields"]["comment"]["comments"]) == 1   # refused once, then accepted once
