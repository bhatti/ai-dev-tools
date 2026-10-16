"""Tests for scripts/common/atlassian_cli.py."""

from __future__ import annotations

import json
import stat

import pytest

from scripts.common.atlassian_cli import write_cli_configs

@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))


CFG = {"JIRA_BASE_URL": "https://example.atlassian.net", "JIRA_EMAIL": "me@example.com", "JIRA_API_TOKEN": "jtok",
       "JIRA_PROJECT": "PROJ", "BITBUCKET_USERNAME": "bbuser@example.com", "BITBUCKET_TOKEN": "bbtok",
       "BITBUCKET_WORKSPACE": "acme"}


def test_acli_has_both_profiles_the_skills_expect(tmp_path):
    jira_cfg, acli_cfg = write_cli_configs(CFG)
    acli = json.loads(acli_cfg.read_text())
    assert acli["default_profile"] == "jira"
    assert acli["profiles"]["jira"] == {"name": "jira", "atlassian_url": CFG["JIRA_BASE_URL"], "email": "me@example.com",
                                        "api_token": "jtok", "defaults": {"project": "PROJ"}}
    assert acli["profiles"]["bitbucket"] == {"name": "bitbucket", "atlassian_url": CFG["JIRA_BASE_URL"], "email": "bbuser@example.com",
                                             "api_token": "bbtok", "defaults": {"workspace": "acme"}}


def test_bitbucket_profile_falls_back_to_the_jira_identity(tmp_path):
    cfg = {k: v for k, v in CFG.items() if not k.startswith("BITBUCKET_")}
    prof = json.loads(write_cli_configs(cfg)[1].read_text())["profiles"]["bitbucket"]
    assert (prof["email"], prof["api_token"]) == ("me@example.com", "jtok")


def test_jira_cli_config_is_yaml(tmp_path):
    import yaml
    data = yaml.safe_load(write_cli_configs(CFG)[0].read_text())
    assert data == {"installation": "cloud", "base-url": CFG["JIRA_BASE_URL"], "login": "me@example.com", "project": "PROJ"}


def test_files_holding_tokens_are_owner_only(tmp_path):
    for path in write_cli_configs(CFG):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_no_jira_url_writes_nothing(tmp_path):
    assert write_cli_configs({"GH_TOKEN": "x"}) == []
    assert not (tmp_path / ".config").exists()


def test_existing_profiles_and_files_are_never_replaced(tmp_path):
    acli = tmp_path / ".config" / "acli" / "config.json"
    acli.parent.mkdir(parents=True)
    acli.write_text(json.dumps({"default_profile": "work", "profiles": {
        "work": {"name": "work", "api_token": "mine"}, "bitbucket": {"name": "bitbucket", "api_token": "laptop-token"}}}))
    jira = tmp_path / ".config" / "jira" / ".config.yml"
    jira.parent.mkdir(parents=True)
    jira.write_text("login: me\n")
    written = write_cli_configs(CFG)
    cfg = json.loads(acli.read_text())
    assert cfg["default_profile"] == "work"
    assert cfg["profiles"]["work"]["api_token"] == "mine" and cfg["profiles"]["bitbucket"]["api_token"] == "laptop-token"
    assert cfg["profiles"]["jira"]["api_token"] == "jtok"  # only the missing profile was added
    assert jira.read_text() == "login: me\n" and jira not in written


def test_unreadable_existing_acli_config_is_left_alone(tmp_path):
    acli = tmp_path / ".config" / "acli" / "config.json"
    acli.parent.mkdir(parents=True)
    acli.write_text("{not json")
    assert acli not in write_cli_configs(CFG)
    assert acli.read_text() == "{not json"


def test_second_run_is_a_no_op(tmp_path):
    write_cli_configs(CFG)
    assert write_cli_configs(CFG) == []
