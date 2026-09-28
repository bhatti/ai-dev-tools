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
    # get_issue_dir returns workspace directly, so logs go to workspace/logs/
    log_path = tmp_workspace / "logs" / "plan.log"
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


def test_write_json_overwrites_readonly_file(sample_config, tmp_workspace):
    """Simulate k8s cross-task permission issue: file owned by another UID."""
    import os, stat
    path = write_json(sample_config, "42", "locked.json", {"v": 1})
    # Make file read-only (simulates file owned by root that UID 1000 can't overwrite)
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    # os.replace() bypasses file permissions — only needs dir write permission
    path2 = write_json(sample_config, "42", "locked.json", {"v": 2})
    assert path2 == path
    result = read_json(sample_config, "42", "locked.json")
    assert result == {"v": 2}


def test_write_text_overwrites_readonly_file(sample_config, tmp_workspace):
    """Same permission scenario for write_text."""
    import stat
    path = write_text(sample_config, "42", "locked.md", "old")
    path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    path2 = write_text(sample_config, "42", "locked.md", "new")
    assert read_text(sample_config, "42", "locked.md") == "new"
