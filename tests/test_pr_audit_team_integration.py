"""Team scoping of the PR audit against the REAL Jira and Bitbucket/GitHub (read-only).

Run with the credentials in the environment:  PYTHONPATH=. pytest tests/test_pr_audit_team_integration.py -m integration -v

  * no --team            no team filter at all, whatever the org configs say
  * --team               the requesting user's default team, looked up in Jira by e-mail
  * --team <name>        exactly that team
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from scripts.analyze import pr_fetcher
from scripts.analyze.run_pr_audit import _apply_scope, _parse_slack_flags
from scripts.common import jira_api
from scripts.common.config import load_config

pytestmark = pytest.mark.integration


ORG_BOARD = (os.environ.get("JIRA_BOARDS") or "").split(",")[0].strip()  # read before the fixture clears the environment


@pytest.fixture
def config(monkeypatch, tmp_path):
    for k in ("JIRA_SPACE", "JIRA_BOARDS", "PR_AUDIT_TEAM_MEMBERS", "PR_AUDIT_FILTER", "PR_AUDIT_GH_MILESTONE", "SLACK_USER_TAG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path))
    c = load_config()
    if not all([c.get("JIRA_BASE_URL"), c.get("JIRA_EMAIL"), c.get("JIRA_API_TOKEN")]):
        pytest.skip("Jira credentials not configured")
    if not (c.get("BITBUCKET_WORKSPACE") and c.get("BITBUCKET_REPO") and c.get("BITBUCKET_TOKEN")):
        pytest.skip("Bitbucket credentials not configured")
    jira_api._user_team_cache.clear()
    return {**c, "DEFAULT_TRACKER": "jira"}


def scoped(config, message, **extra):
    cfg = {**config, **extra}
    _apply_scope(cfg, _parse_slack_flags({"SLACK_MESSAGE": message}), "jira")
    return cfg


class CountingJira:
    """Counts the real Jira searches the fetcher makes."""

    def __enter__(self):
        self.calls = []
        real = pr_fetcher._jira_search_issues
        self._patch = patch.object(pr_fetcher, "_jira_search_issues", side_effect=lambda cfg, jql, **k: (self.calls.append(jql) or real(cfg, jql, **k)))
        self._patch.start()
        return self

    def __exit__(self, *a):
        self._patch.stop()


def test_the_default_team_of_a_person_is_found_by_e_mail_and_matches_the_credentials_team(config):
    mine = jira_api.resolve_current_user_team(config)
    assert mine, "the credentials' account has no team on its recent issues: pick another account for this test"
    jira_api._user_team_cache.clear()
    assert jira_api.resolve_user_team(config, config["JIRA_EMAIL"]) == mine


def test_an_unknown_person_has_no_default_team(config):
    assert jira_api.resolve_user_team(config, "no.such.person.zz@example.invalid") is None


def test_without_team_nothing_is_filtered_even_with_org_defaults(config):
    """The org configs of a real deployment (board, team, members) must not scope the audit unless --team is given."""
    cfg = scoped(config, "--n-prs 5", JIRA_SPACE="Some Org Team", JIRA_BOARDS=config.get("JIRA_BOARDS") or "1", PR_AUDIT_TEAM_MEMBERS="someone")
    assert (cfg["JIRA_SPACE"], cfg["JIRA_BOARDS"], cfg["PR_AUDIT_TEAM_MEMBERS"]) == ("", "", "")
    with CountingJira() as jira:
        prs = pr_fetcher.fetch_prs(cfg, 5)
    assert jira.calls == [] and 0 < len(prs) <= 5
    assert pr_fetcher._resolve_jira_issue_keys(cfg) is None


def test_bare_team_filters_by_the_default_team_of_the_slack_user(config):
    team = jira_api.resolve_current_user_team(config)
    jira_api._user_team_cache.clear()
    cfg = scoped(config, "--team", JIRA_SPACE="Some Org Team", SLACK_USER_TAG=config["JIRA_EMAIL"])
    assert cfg["JIRA_SPACE"] == team, "the org default must not win over the Slack user's own team"
    with CountingJira() as jira:
        prs = pr_fetcher.fetch_prs(cfg, 5)
    assert jira.calls and team in jira.calls[0].replace("\\", "") and len(prs) <= 5


def test_bare_team_for_an_unknown_slack_user_filters_nothing(config):
    cfg = scoped(config, "--team", SLACK_USER_TAG="no.such.person.zz@example.invalid")
    assert cfg["JIRA_SPACE"] == ""
    with CountingJira() as jira:
        prs = pr_fetcher.fetch_prs(cfg, 5)
    assert jira.calls == [] and prs


def test_a_named_team_is_used_exactly(config):
    team = jira_api.resolve_current_user_team(config)
    cfg = scoped(config, f'--team "{team}"', JIRA_SPACE="Some Org Team", SLACK_USER_TAG="someone.else@example.invalid")
    assert cfg["JIRA_SPACE"] == team  # exactly what was typed, not the org default and not anyone's default team
    with CountingJira() as jira:
        pr_fetcher.fetch_prs(cfg, 5)
    assert jira.calls and team in jira.calls[0].replace("\\", "")


def test_a_board_alone_does_not_pull_in_a_team(config):
    board = ORG_BOARD
    if not board:
        pytest.skip("JIRA_BOARDS not configured")
    cfg = scoped(config, f"--board {board}")
    assert cfg["JIRA_BOARDS"] == board and cfg["JIRA_SPACE"] == ""
    with patch.object(jira_api, "resolve_current_user_team", side_effect=AssertionError("a board must not look up a team")):
        keys, _ = pr_fetcher._resolve_jira_issue_keys(cfg)
    assert keys
