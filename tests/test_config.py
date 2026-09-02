"""Tests for scripts/common/config.py"""

import os
import sys

import pytest

from scripts.common.config import DEFAULTS, get_issue_dir, get_workspace_dir, load_config, validate_claude_config


def test_load_config_applies_defaults(tmp_workspace):
    config = load_config()
    assert config["PICKUP_LABEL"] == "ai-ready"
    assert config["INPROGRESS_LABEL"] == "ai-in-progress"
    assert config["MAX_ISSUES"] == "5"


def test_load_config_env_overrides_default(tmp_workspace, monkeypatch):
    monkeypatch.setenv("PICKUP_LABEL", "custom-label")
    config = load_config()
    assert config["PICKUP_LABEL"] == "custom-label"


def test_load_config_required_present(monkeypatch):
    monkeypatch.setenv("GH_ORG", "myorg")
    config = load_config(required=["GH_ORG"])
    assert config["GH_ORG"] == "myorg"


def test_load_config_required_missing_exits(monkeypatch):
    monkeypatch.delenv("GH_ORG", raising=False)
    with pytest.raises(SystemExit) as exc:
        load_config(required=["GH_ORG"])
    assert exc.value.code == 1


def test_get_workspace_dir(tmp_workspace):
    config = {"WORKSPACE_DIR": str(tmp_workspace)}
    result = get_workspace_dir(config)
    assert result == tmp_workspace


def test_get_issue_dir_creates_directory(tmp_workspace):
    config = {"WORKSPACE_DIR": str(tmp_workspace)}
    issue_dir = get_issue_dir(config, "99")
    assert issue_dir.exists()
    # get_issue_dir returns workspace directly — each task runs in its own pod
    assert issue_dir == tmp_workspace


def test_get_issue_dir_idempotent(tmp_workspace):
    config = {"WORKSPACE_DIR": str(tmp_workspace)}
    d1 = get_issue_dir(config, "99")
    d2 = get_issue_dir(config, "99")
    assert d1 == d2


def test_github_alias_resolves(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GH_ORG", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_alias")
    monkeypatch.setenv("GITHUB_ORG", "alias-org")
    monkeypatch.setenv("GITHUB_REPO", "alias-repo")
    config = load_config()
    assert config["GH_TOKEN"] == "ghp_alias"
    assert config["GH_ORG"] == "alias-org"
    assert config["GH_REPO"] == "alias-repo"


def test_canonical_wins_over_alias(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "canonical")
    monkeypatch.setenv("GITHUB_TOKEN", "alias")
    config = load_config()
    assert config["GH_TOKEN"] == "canonical"


def test_bb_alias_resolves(monkeypatch):
    monkeypatch.delenv("BITBUCKET_REPO", raising=False)
    monkeypatch.delenv("BITBUCKET_TOKEN", raising=False)
    monkeypatch.setenv("BB_REPO", "bb-repo")
    monkeypatch.setenv("BB_TOKEN", "bb-token")
    config = load_config()
    assert config["BITBUCKET_REPO"] == "bb-repo"
    assert config["BITBUCKET_TOKEN"] == "bb-token"


# --- validate_claude_config ---

def test_validate_claude_config_bedrock_mode(capsys):
    config = {"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_BEDROCK_BASE_URL": "http://ai/bedrock"}
    validate_claude_config(config)
    out = capsys.readouterr().out
    assert "mode=bedrock" in out
    assert "http://ai/bedrock" in out


def test_validate_claude_config_bedrock_strips_credentials(capsys):
    config = {"CLAUDE_CODE_USE_BEDROCK": "1", "ANTHROPIC_BEDROCK_BASE_URL": "http://user:secret@proxy/bedrock"}
    validate_claude_config(config)
    out = capsys.readouterr().out
    assert "secret" not in out
    assert "mode=bedrock" in out


def test_validate_claude_config_direct_api_key(capsys):
    config = {"CLAUDE_CODE_USE_BEDROCK": "0", "ANTHROPIC_API_KEY": "sk-ant-test"}
    validate_claude_config(config)
    out = capsys.readouterr().out
    assert "mode=direct-api-key" in out


def test_validate_claude_config_missing_exits(capsys):
    config = {"CLAUDE_CODE_USE_BEDROCK": "0", "ANTHROPIC_API_KEY": ""}
    with pytest.raises(SystemExit) as exc:
        validate_claude_config(config)
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Claude API not configured" in err


def test_validate_claude_config_bedrock_default_url(capsys):
    # ANTHROPIC_BEDROCK_BASE_URL absent — should use the hardcoded default
    config = {"CLAUDE_CODE_USE_BEDROCK": "1"}
    validate_claude_config(config)
    out = capsys.readouterr().out
    assert "mode=bedrock" in out


# --- COMPLEXITY_MODEL_MAP ---

def test_complexity_model_map_has_required_tiers():
    from scripts.common.config import COMPLEXITY_MODEL_MAP, MODEL_BEDROCK_HAIKU, MODEL_BEDROCK_SONNET, MODEL_BEDROCK_OPUS
    assert COMPLEXITY_MODEL_MAP["low"] == MODEL_BEDROCK_HAIKU
    assert COMPLEXITY_MODEL_MAP["medium"] == MODEL_BEDROCK_SONNET
    assert COMPLEXITY_MODEL_MAP["high"] == MODEL_BEDROCK_OPUS


def test_complexity_model_map_covers_all_tiers():
    from scripts.common.config import COMPLEXITY_MODEL_MAP
    assert set(COMPLEXITY_MODEL_MAP.keys()) == {"low", "medium", "high"}


def test_complexity_model_map_values_are_nonempty_strings():
    from scripts.common.config import COMPLEXITY_MODEL_MAP
    for tier, model_id in COMPLEXITY_MODEL_MAP.items():
        assert isinstance(model_id, str) and model_id, f"tier={tier!r} has empty model ID"


# --- .env file loading ---

def test_load_config_reads_dotenv_when_present(tmp_path, monkeypatch):
    """load_config reads KEY=VALUE from a .env file in cwd when key is absent from OS env."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("DEFAULT_TRACKER=jira\nSOME_VAR=test_value\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DEFAULT_TRACKER", raising=False)
    monkeypatch.delenv("SOME_VAR", raising=False)

    config = load_config()
    assert config["DEFAULT_TRACKER"] == "jira"
    assert config["SOME_VAR"] == "test_value"


def test_load_config_env_wins_over_dotenv(tmp_path, monkeypatch):
    """.env value is ignored when OS env already sets the same key."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("DEFAULT_TRACKER=jira\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DEFAULT_TRACKER", "github")

    config = load_config()
    assert config["DEFAULT_TRACKER"] == "github"


def test_load_config_dotenv_ignores_comments_and_blanks(tmp_path, monkeypatch):
    """.env parser skips comment lines and blank lines."""
    dotenv = tmp_path / ".env"
    dotenv.write_text("# this is a comment\n\nTRACKER_KEY=from_dotenv\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRACKER_KEY", raising=False)

    config = load_config()
    assert config["TRACKER_KEY"] == "from_dotenv"


def test_load_config_dotenv_strips_quotes(tmp_path, monkeypatch):
    """.env parser strips surrounding quotes from values."""
    dotenv = tmp_path / ".env"
    dotenv.write_text('MY_KEY="quoted_value"\nOTHER_KEY=\'single_quoted\'\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MY_KEY", raising=False)
    monkeypatch.delenv("OTHER_KEY", raising=False)

    config = load_config()
    assert config["MY_KEY"] == "quoted_value"
    assert config["OTHER_KEY"] == "single_quoted"


def test_load_config_no_dotenv_is_fine(tmp_path, monkeypatch):
    """load_config doesn't fail when no .env file exists."""
    monkeypatch.chdir(tmp_path)  # empty dir, no .env
    config = load_config()
    assert "WORKSPACE_DIR" in config


# --- CamelCase org config aliases ---

def test_camelcase_jira_aliases(monkeypatch):
    monkeypatch.delenv("JIRA_BASE_URL", raising=False)
    monkeypatch.delenv("JIRA_PROJECT", raising=False)
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.setenv("JiraUrl", "https://company.atlassian.net")
    monkeypatch.setenv("JiraProject", "PROJ")
    monkeypatch.setenv("JiraEmail", "user@example.com")
    monkeypatch.setenv("JiraApiToken", "secret")
    config = load_config()
    assert config["JIRA_BASE_URL"] == "https://company.atlassian.net"
    assert config["JIRA_PROJECT"] == "PROJ"
    assert config["JIRA_EMAIL"] == "user@example.com"
    assert config["JIRA_API_TOKEN"] == "secret"


def test_camelcase_bitbucket_aliases(monkeypatch):
    monkeypatch.delenv("BITBUCKET_WORKSPACE", raising=False)
    monkeypatch.delenv("BITBUCKET_REPO", raising=False)
    monkeypatch.delenv("BITBUCKET_TOKEN", raising=False)
    monkeypatch.setenv("BitbucketWorkspace", "myworkspace")
    monkeypatch.setenv("BitbucketRepo", "myrepo")
    monkeypatch.setenv("BitbucketToken", "ATATT_fake")
    config = load_config()
    assert config["BITBUCKET_WORKSPACE"] == "myworkspace"
    assert config["BITBUCKET_REPO"] == "myrepo"
    assert config["BITBUCKET_TOKEN"] == "ATATT_fake"


def test_camelcase_github_aliases(monkeypatch):
    monkeypatch.delenv("GH_ORG", raising=False)
    monkeypatch.delenv("GH_REPO", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)  # clear existing alias too
    monkeypatch.setenv("GitHubOrg", "myorg")
    monkeypatch.setenv("GitHubRepo", "myrepo")
    monkeypatch.setenv("GitHubToken", "ghp_fake")
    config = load_config()
    assert config["GH_ORG"] == "myorg"
    assert config["GH_REPO"] == "myrepo"
    assert config["GH_TOKEN"] == "ghp_fake"


def test_camelcase_slack_aliases(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL", raising=False)
    monkeypatch.delenv("SLACK_THREAD_TS", raising=False)
    monkeypatch.setenv("SlackToken", "xoxb-fake")
    monkeypatch.setenv("SlackChannel", "standup")
    monkeypatch.setenv("SlackThreadTs", "1234567890.123")
    config = load_config()
    assert config["SLACK_BOT_TOKEN"] == "xoxb-fake"
    assert config["SLACK_CHANNEL"] == "standup"
    assert config["SLACK_THREAD_TS"] == "1234567890.123"


def test_camelcase_claude_aliases(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SKIP_BEDROCK_AUTH", raising=False)
    monkeypatch.delenv("ANTHROPIC_BEDROCK_BASE_URL", raising=False)
    monkeypatch.delenv("EXTRA_SKILLS_REPOS", raising=False)
    monkeypatch.setenv("ClaudeUseBedrock", "1")
    monkeypatch.setenv("ClaudeSkipBedrockAuth", "1")
    monkeypatch.setenv("AnthropicBedrockBaseUrl", "http://ai/bedrock")
    monkeypatch.setenv("ExtraSkillsRepos", "/opt/skills")
    config = load_config()
    assert config["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert config["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] == "1"
    assert config["ANTHROPIC_BEDROCK_BASE_URL"] == "http://ai/bedrock"
    assert config["EXTRA_SKILLS_REPOS"] == "/opt/skills"


def test_camelcase_canonical_wins(monkeypatch):
    """UPPER_SNAKE canonical always wins over CamelCase alias."""
    monkeypatch.setenv("JIRA_BASE_URL", "https://canonical.atlassian.net")
    monkeypatch.setenv("JiraUrl", "https://alias.atlassian.net")
    config = load_config()
    assert config["JIRA_BASE_URL"] == "https://canonical.atlassian.net"
