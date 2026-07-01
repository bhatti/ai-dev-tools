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
    assert issue_dir == tmp_workspace / "99"


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
