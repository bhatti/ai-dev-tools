"""Tests for scripts/common/label_utils.py"""

import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from scripts.common.label_utils import (
    gh_add_label,
    gh_has_label,
    gh_remove_label,
    gh_transition_label,
    jira_add_label,
    jira_transition_label,
)


# ── GitHub tests ─────────────────────────────────────────────────────────────

@patch("scripts.common.label_utils._run")
def test_gh_add_label_calls_rest_api(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    gh_add_label("org", "repo", "42", "ai-ready")
    mock_run.assert_called_once_with(
        ["gh", "api", "--method", "POST",
         "repos/org/repo/issues/42/labels", "-f", "labels[]=ai-ready"],
        check=False,
    )


@patch("scripts.common.label_utils._run")
def test_gh_add_label_raises_on_failure(mock_run):
    mock_run.return_value = MagicMock(returncode=1, stderr="Resource not accessible")
    with pytest.raises(RuntimeError, match="Could not add label"):
        gh_add_label("org", "repo", "42", "ai-ready")


@patch("scripts.common.label_utils._run")
def test_gh_remove_label_calls_rest_api(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    gh_remove_label("org", "repo", "42", "ai-ready")
    mock_run.assert_called_once_with(
        ["gh", "api", "--method", "DELETE",
         "repos/org/repo/issues/42/labels/ai-ready"],
        check=False,
    )


@patch("scripts.common.label_utils.gh_add_label")
@patch("scripts.common.label_utils.gh_remove_label")
def test_gh_transition_label(mock_remove, mock_add):
    gh_transition_label("org", "repo", "42", "ai-ready", "ai-in-progress")
    mock_remove.assert_called_once_with("org", "repo", "42", "ai-ready")
    mock_add.assert_called_once_with("org", "repo", "42", "ai-in-progress")


@patch("scripts.common.label_utils._run")
def test_gh_has_label_returns_true(mock_run):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='[{"name": "ai-ready"}, {"name": "bug"}]',
    )
    assert gh_has_label("org", "repo", "42", "ai-ready") is True


@patch("scripts.common.label_utils._run")
def test_gh_has_label_returns_false(mock_run):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='[{"name": "bug"}]',
    )
    assert gh_has_label("org", "repo", "42", "ai-ready") is False


# ── Jira tests ────────────────────────────────────────────────────────────────

@patch("scripts.common.label_utils.jira_add_label_api")
def test_jira_add_label_delegates_to_api(mock_api):
    config = {"JIRA_BASE_URL": "https://jira.example.com", "JIRA_EMAIL": "x", "JIRA_API_TOKEN": "y"}
    jira_add_label(config, "PROJ-1", "ai-ready")
    mock_api.assert_called_once_with(config, "PROJ-1", "ai-ready")


@patch("scripts.common.label_utils.jira_transition_label_api")
def test_jira_transition_label_delegates_to_api(mock_api):
    config = {"JIRA_BASE_URL": "https://jira.example.com", "JIRA_EMAIL": "x", "JIRA_API_TOKEN": "y"}
    jira_transition_label(config, "PROJ-1", "ai-ready", "ai-in-progress")
    mock_api.assert_called_once_with(config, "PROJ-1", "ai-ready", "ai-in-progress")
