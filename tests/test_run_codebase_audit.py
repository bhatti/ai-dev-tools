"""Tests for scripts/analyze/run_codebase_audit.py"""

import os
from unittest.mock import patch

import pytest

from scripts.analyze.run_codebase_audit import _parse_slack_flags


class TestParseSlackFlags:
    def test_empty_message_returns_defaults(self):
        result = _parse_slack_flags({})
        assert result["n_commits"] is None
        assert result["max_size"] is None
        assert result["full_report"] is False

    def test_commits_natural_language(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit last 500 commits"})
        assert result["n_commits"] == 500

    def test_commits_flag(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "--commits 200"})
        assert result["n_commits"] == 200

    def test_max_size_mb(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit max size 2MB"})
        assert result["max_size"] == 2 * 1024 * 1024

    def test_max_size_kb(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit max size 512KB"})
        assert result["max_size"] == 512 * 1024

    def test_full_flag_detected(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "codebase-audit --full"})
        assert result["full_report"] is True

    def test_full_flag_absent(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "codebase-audit last 200 commits"})
        assert result["full_report"] is False

    def test_commits_and_full(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "last 500 commits --full"})
        assert result["n_commits"] == 500
        assert result["full_report"] is True

    def test_max_size_and_full(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit max size 2MB --full"})
        assert result["max_size"] == 2 * 1024 * 1024
        assert result["full_report"] is True

    def test_env_fallback(self):
        with patch.dict(os.environ, {"SLACK_MESSAGE": "last 100 commits"}):
            result = _parse_slack_flags({})
            assert result["n_commits"] == 100

    def test_returns_dict_not_tuple(self):
        result = _parse_slack_flags({"SLACK_MESSAGE": "audit"})
        assert isinstance(result, dict)
        assert set(result.keys()) == {"n_commits", "max_size", "full_report"}
