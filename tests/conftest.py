"""Shared test fixtures."""

import json
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def tmp_workspace(tmp_path):
    """Provide a temporary workspace directory and set WORKSPACE_DIR env var."""
    old = os.environ.get("WORKSPACE_DIR")
    os.environ["WORKSPACE_DIR"] = str(tmp_path)
    yield tmp_path
    if old is None:
        os.environ.pop("WORKSPACE_DIR", None)
    else:
        os.environ["WORKSPACE_DIR"] = old


@pytest.fixture
def sample_issue():
    return {
        "id": "42",
        "number": 42,
        "title": "Add user authentication",
        "body": "We need OAuth2 login support",
        "url": "https://github.com/org/repo/issues/42",
        "org": "org",
        "repo": "repo",
        "labels": ["ai-ready"],
        "source": "github",
    }


@pytest.fixture
def sample_config(tmp_workspace):
    return {
        "WORKSPACE_DIR": str(tmp_workspace),
        "AI_MODEL": "claude-sonnet-4-6",
        "MAX_TURNS_PLAN": "30",
        "MAX_TURNS_IMPLEMENT": "100",
        "PICKUP_LABEL": "ai-ready",
        "INPROGRESS_LABEL": "ai-in-progress",
        "PR_OPEN_LABEL": "ai-pr-open",
        "NEEDS_HUMAN_LABEL": "needs-human",
        "GIT_USER_NAME": "Test Agent",
        "GIT_USER_EMAIL": "test@example.com",
        "MAX_ISSUES": "5",
        "POLL_INTERVAL": "120",
        "GH_ORG": "testorg",
        "GH_REPO": "testrepo",
        "GH_TOKEN": "ghp_test",
    }
