"""Tests for scripts/common/docker_check.py (a fake `docker` executable on PATH stands in for the daemon)."""

from __future__ import annotations

import os
import stat


from scripts.common import docker_check as dc


def install_fake_docker(tmp_path, monkeypatch, body: str) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "docker"
    script.write_text(f"#!/bin/bash\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def test_ok_reports_server_version(tmp_path, monkeypatch):
    install_fake_docker(tmp_path, monkeypatch, "echo 27.0.1")
    ok, msg = dc.docker_preflight(timeout=2)
    assert ok is True and "27.0.1" in msg


def test_daemon_down_message_mentions_dind(tmp_path, monkeypatch):
    install_fake_docker(tmp_path, monkeypatch, "echo 'Cannot connect to the Docker daemon' >&2; exit 1")
    ok, msg = dc.docker_preflight(timeout=0.3)
    assert ok is False and msg == dc.DIND_HINT and "--dind" in msg


def test_missing_cli_is_reported_immediately(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    ok, msg = dc.docker_preflight(timeout=30)
    assert ok is False and "not found" in msg


def test_daemon_that_comes_up_during_the_wait_is_accepted(tmp_path, monkeypatch):
    counter = tmp_path / "calls"
    install_fake_docker(tmp_path, monkeypatch,
                        f'n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{counter}"; '
                        '[ "$n" -ge 3 ] && echo 29.9.0 || exit 1')
    ok, msg = dc.docker_preflight(timeout=10)
    assert ok is True and "29.9.0" in msg and counter.read_text().strip() == "3"
