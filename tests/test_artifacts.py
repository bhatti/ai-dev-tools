"""Tests for scripts/common/artifacts.py"""

import pytest

from scripts.common.artifacts import (
    list_artifacts,
    read_json,
    read_text,
    write_json,
    write_log,
    write_text,
)


def test_write_and_read_json(sample_config, tmp_workspace):
    data = {"status": "DONE", "count": 3}
    path = write_json(sample_config, "42", "plan_result.json", data)
    assert path.exists()

    result = read_json(sample_config, "42", "plan_result.json")
    assert result == data


def test_read_json_missing_returns_none(sample_config):
    result = read_json(sample_config, "99", "nonexistent.json")
    assert result is None


def test_write_and_read_text(sample_config):
    write_text(sample_config, "42", "plan.md", "# My Plan\n\nStep 1")
    result = read_text(sample_config, "42", "plan.md")
    assert result == "# My Plan\n\nStep 1"


def test_read_text_missing_returns_none(sample_config):
    result = read_text(sample_config, "99", "nonexistent.md")
    assert result is None


def test_write_log_creates_logs_subdir(sample_config, tmp_workspace):
    write_log(sample_config, "42", "plan", "log output here")
    log_path = tmp_workspace / "42" / "logs" / "plan.log"
    assert log_path.exists()
    assert log_path.read_text() == "log output here"


def test_list_artifacts(sample_config, tmp_workspace):
    write_json(sample_config, "42", "issue.json", {"id": "42"})
    write_text(sample_config, "42", "plan.md", "# Plan")
    write_log(sample_config, "42", "plan", "log")

    artifacts = list_artifacts(sample_config, "42")
    assert "issue.json" in artifacts
    assert "plan.md" in artifacts
    assert "logs/plan.log" in artifacts


def test_write_json_overwrites(sample_config):
    write_json(sample_config, "42", "result.json", {"status": "PENDING"})
    write_json(sample_config, "42", "result.json", {"status": "DONE"})
    result = read_json(sample_config, "42", "result.json")
    assert result["status"] == "DONE"
