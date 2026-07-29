"""Tests for scripts/common/entrypoint.py"""

import json
import os
import sys

import pytest

from scripts.common.entrypoint import run_main


def test_run_main_success(tmp_workspace):
    called = []

    def good_main():
        called.append(True)

    run_main(good_main, "result.json")
    assert called == [True]
    assert not (tmp_workspace / "result.json").exists()


def test_run_main_writes_error_artifact_on_exception(tmp_workspace):
    def bad_main():
        raise RuntimeError("something broke")

    with pytest.raises(SystemExit) as exc_info:
        run_main(bad_main, "result.json")

    assert exc_info.value.code == 1
    artifact = tmp_workspace / "result.json"
    assert artifact.exists()
    data = json.loads(artifact.read_text())
    assert data["status"] == "ERROR"
    assert "something broke" in data["reason"]


def test_run_main_sys_exit_propagates(tmp_workspace):
    """sys.exit() is BaseException, not Exception — must not be caught."""
    def exits_main():
        sys.exit(42)

    with pytest.raises(SystemExit) as exc_info:
        run_main(exits_main, "result.json")

    assert exc_info.value.code == 42
    # No spurious artifact should be written for a clean sys.exit
    assert not (tmp_workspace / "result.json").exists()


def test_run_main_jira_auth_error_writes_artifact(tmp_workspace):
    def auth_fail():
        raise RuntimeError("Failed to authenticate with Jira at https://host — check credentials")

    with pytest.raises(SystemExit):
        run_main(auth_fail, "gather_result.json")

    data = json.loads((tmp_workspace / "gather_result.json").read_text())
    assert data["status"] == "ERROR"
    assert "Jira" in data["reason"]
