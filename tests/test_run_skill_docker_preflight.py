"""docker_blocker / docker_required in scripts/skill/run_skill.py: fail fast instead of a misleading run."""

from __future__ import annotations

import pytest

from scripts.common.docker_check import DIND_HINT
from scripts.skill import run_skill as rs
from scripts.skill.flags import parse_skill_flags


@pytest.fixture()
def daemon(monkeypatch):
    """Control what the (module-level) docker preflight reports; records the timeout it was called with."""
    state = {"result": (True, "docker daemon 27.0"), "timeouts": [], "hosts": []}

    def fake(timeout=30.0, host=None):
        state["timeouts"].append(timeout)
        state["hosts"].append(host)
        return state["result"]

    monkeypatch.setattr(rs, "docker_preflight", fake)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    return state


class TestDockerRequired:
    @pytest.mark.parametrize("text", ["--image app:1", "run --baseline-image app:0 --image app:1", "--compose docker-compose.yml",
                                      "--kind docker", "x --compose-file c.yml"])
    def test_detects_container_flags(self, text):
        assert rs.docker_required(text) is True

    @pytest.mark.parametrize("text", ["", "--url http://x", "--images-dir foo", "use the image processing code", "--baseline-url http://b",
                                      "--image-pull-policy Always", "--compose-mode x", "--kind docker-ish"])
    def test_ignores_others(self, text):
        assert rs.docker_required(text) is False


class TestDockerBlocker:
    def test_not_needed_never_calls_preflight(self, daemon):
        flags = parse_skill_flags("ygs-qa https://github.com/o/r/pull/1 --url http://x")
        assert rs.docker_blocker(flags) is None and daemon["timeouts"] == []

    def test_image_flag_without_daemon_blocks_with_dind_hint(self, daemon):
        daemon["result"] = (False, DIND_HINT)
        msg = rs.docker_blocker(parse_skill_flags("ygs-qa https://github.com/o/r/pull/1 --image app:1"))
        assert msg and "--dind" in msg and "--image/--compose" in msg

    def test_image_flag_with_daemon_passes(self, daemon):
        assert rs.docker_blocker(parse_skill_flags("ygs-qa x --image a")) is None

    def test_dind_requested_but_daemon_missing_says_sidecar_problem(self, daemon):
        daemon["result"] = (False, DIND_HINT)
        msg = rs.docker_blocker(parse_skill_flags("qa-regression --dind"))
        assert msg and "was requested" in msg and "docker-dind sidecar" in msg and "tcp://localhost:2375" in msg

    def test_dind_waits_long_and_probes_the_sidecar_address_even_without_docker_host(self, daemon):
        """--dind without DOCKER_HOST in this process must still probe tcp://localhost:2375, not the default socket."""
        rs.docker_blocker(parse_skill_flags("ygs-qa x --dind"))
        assert daemon["timeouts"] == [90] and daemon["hosts"] == ["tcp://localhost:2375"]

    def test_configured_docker_host_is_what_gets_probed(self, daemon, monkeypatch):
        monkeypatch.setenv("DOCKER_HOST", "tcp://dind:2375")
        rs.docker_blocker(parse_skill_flags("ygs-qa x --dind"))
        assert daemon["hosts"] == ["tcp://dind:2375"]

    def test_an_ambient_docker_host_alone_never_blocks_skills_that_do_not_need_docker(self, daemon, monkeypatch):
        monkeypatch.setenv("DOCKER_HOST", "tcp://localhost:2375")
        daemon["result"] = (False, DIND_HINT)
        assert rs.docker_blocker(parse_skill_flags("ygs-review-pr https://github.com/o/r/pull/1")) is None and daemon["timeouts"] == []

    def test_missing_cli_message_is_not_wrapped_in_a_daemon_story(self, daemon):
        daemon["result"] = (False, "docker CLI not found in this environment")
        assert rs.docker_blocker(parse_skill_flags("qa --dind")) == "docker CLI not found in this environment"

    def test_without_request_only_short_wait(self, daemon):
        rs.docker_blocker(parse_skill_flags("ygs-qa x --image a"))
        assert daemon["timeouts"] == [10] and daemon["hosts"] == [None]


class TestCloneKeepsTokensOutOfTheCheckout:
    """_clone_target_repo must not leave the token in .git/config: the checkout is read by code we did not write."""

    @pytest.fixture()
    def calls(self, monkeypatch):
        seen = []
        monkeypatch.setattr(rs, "resolve_clone_auth", lambda config, tracker: ("tok", "x-token-auth", ""))
        monkeypatch.setattr(rs, "clone_repo", lambda *a, **k: seen.append(("clone", k.get("http_token"))))
        monkeypatch.setattr(rs, "create_branch", lambda dest, branch: seen.append(("branch", branch)) or branch)
        monkeypatch.setattr(rs, "drop_stored_credentials", lambda dest, url, user: seen.append(("drop", url, user)))
        return seen

    def test_credentials_are_dropped_after_the_branch_is_checked_out(self, tmp_path, calls):
        dest = rs._clone_target_repo("https://bitbucket.org/ws/repo.git", "dev", {}, "jira", tmp_path)
        assert dest == tmp_path / "repo"
        assert [c[0] for c in calls] == ["clone", "branch", "drop"] and calls[2] == ("drop", "https://bitbucket.org/ws/repo.git", "x-token-auth")

    def test_a_failure_to_remove_them_fails_the_clone_instead_of_leaving_the_token(self, tmp_path, calls, monkeypatch):
        def boom(*a):
            raise RuntimeError("git config failed")
        monkeypatch.setattr(rs, "drop_stored_credentials", boom)
        assert rs._clone_target_repo("https://bitbucket.org/ws/repo.git", "dev", {}, "jira", tmp_path) is None

    def test_nothing_to_drop_without_a_token(self, tmp_path, calls, monkeypatch):
        monkeypatch.setattr(rs, "resolve_clone_auth", lambda config, tracker: ("", "", ""))
        rs._clone_target_repo("https://github.com/o/r.git", "main", {}, "github", tmp_path)
        assert [c[0] for c in calls] == ["clone", "branch"]


class TestRealPreflightPath:
    """--dind must reach the sidecar's address even when DOCKER_HOST is unset (a fake `docker` stands in for the daemon)."""

    def test_dind_probes_the_sidecar_address_not_the_default_socket(self, tmp_path, monkeypatch):
        import os
        import stat
        bindir = tmp_path / "bin"
        bindir.mkdir()
        shim = bindir / "docker"
        shim.write_text('#!/bin/bash\n[ "$DOCKER_HOST" = "tcp://localhost:2375" ] && { echo 27.0; exit 0; }\necho "no daemon at ${DOCKER_HOST:-default socket}" >&2; exit 1\n')
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.delenv("DOCKER_HOST", raising=False)
        assert rs.docker_blocker(parse_skill_flags("ygs-qa x --dind")) is None


from scripts.skill.run_skill import job_failure  # noqa: E402


class TestJobFailure:
    @pytest.mark.parametrize("status", ["ERROR", "MAX_TURNS_REACHED"])
    def test_error_statuses_fail_the_job(self, status):
        assert job_failure(status, {}) == status

    def test_blocked_signal_fails_even_when_the_status_word_is_done(self):
        why = job_failure("DONE", {"status": "DONE", "signal": "BLOCKED", "summary": "docker daemon unreachable"})
        assert why and "docker daemon unreachable" in why

    def test_blocked_status_word_fails_the_job(self):
        assert job_failure("BLOCKED", {"status": "BLOCKED", "reason": "no access"})

    @pytest.mark.parametrize("data", [{"status": "DONE"}, {"status": "DONE", "signal": "DONE_WITH_CONCERNS"}])
    def test_normal_completion_passes(self, data):
        assert job_failure("DONE", data) is None
