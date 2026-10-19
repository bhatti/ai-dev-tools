"""Tests for scripts/qa/env_manager.py. A fake `docker` shim stands in for the daemon; git is injected."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.common.docker_check import DIND_HINT
from scripts.qa import env_manager as em


class Ok(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        code = 503 if self.path == "/warming" else 200
        self.send_response(code)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


@pytest.fixture()
def app():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Ok)
    threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.01), daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture()
def fake_docker(tmp_path, monkeypatch, app):
    """A `docker` executable that logs its argv and answers like a healthy daemon publishing `app`'s port."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    port = app.rsplit(":", 1)[1]
    script = bindir / "docker"
    script.write_text(f"""#!/bin/bash
echo "$@" >> "{log}"
echo "$1 GH_TOKEN=${{GH_TOKEN-unset}} NPM_TOKEN=${{NPM_TOKEN-unset}}" >> "{log.parent}/docker.env"
case "$1" in
  info) case "$*" in *Architecture*) echo "${{FAKE_DAEMON_ARCH:-x86_64}}"; exit 0 ;; esac; exit ${{FAKE_DOCKER_INFO_RC:-0}} ;;
  image) if [ -f "{log}.pulled" ] || [ -n "$FAKE_IMAGE_PRESENT" ]; then case "$*" in *Architecture*) echo "${{FAKE_IMAGE_ARCH-amd64}}" ;; *) echo '{{"8080/tcp":{{}}}}' ;; esac; else exit 1; fi ;;
  pull) if [ -n "$FAKE_PULL_FAIL" ]; then echo "${{FAKE_PULL_MSG:-pull access denied}}" >&2; exit 1; fi; touch "{log}.pulled" ;;
  run) if [ -n "$FAKE_RUN_FAIL" ]; then echo "port is already allocated" >&2; exit 1; fi; echo fakecid ;;
  port) [ -n "$FAKE_PORT_EMPTY" ] || echo "127.0.0.1:{port}" ;;
  inspect) echo true ;;
  logs) echo "container log line" ;;
  rm) echo "Error: removal of container is already in progress" >&2; exit ${{FAKE_RM_RC:-0}} ;;
  compose) case "$*" in *" down "*) [ -f "{log}.downfail" ] && exit 1; exit 0 ;; esac; exit ${{FAKE_COMPOSE_RC:-0}} ;;
esac
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return log


class TestWaitReady:
    def test_ready_immediately(self, app):
        em.wait_ready(app, "/", timeout=5)

    def test_5xx_is_not_ready_until_deadline(self, app):
        with pytest.raises(em.EnvError, match="HTTP 503"):
            em.wait_ready(app, "/warming", timeout=0.3)

    def test_connection_refused_reports_last_error(self):
        with pytest.raises(em.EnvError, match="not ready after"):
            em.wait_ready("http://127.0.0.1:1", "/", timeout=0.3)

    def test_dead_process_aborts_early(self, app):
        with pytest.raises(em.EnvError, match="exited before"):
            em.wait_ready("http://127.0.0.1:1", "/", timeout=30, alive=lambda: False)



class TestUrlEnv:
    def test_run_exports_env_and_returns_child_exit_code(self, tmp_path, app):
        spec = em.EnvSpec(kind="url", url=app, side="baseline", out_dir=str(tmp_path))
        child = f"import os, sys; sys.exit(7 if os.environ['QA_BASE_URL'] == {app!r} and os.environ['QA_SIDE'] == 'baseline' "\
                f"and os.environ['QA_OUT'].endswith('baseline') else 1)"
        assert em.run_in_env(spec, [sys.executable, "-c", child]) == 7
        assert (tmp_path / "baseline" / "logs" / "env.log").exists()

    def test_not_ready_is_blocked_with_failure_file(self, tmp_path):
        spec = em.EnvSpec(kind="url", url="http://127.0.0.1:1", ready_timeout=0.3, out_dir=str(tmp_path))
        assert em.run_in_env(spec, [sys.executable, "-c", "raise SystemExit(0)"]) == em.EXIT_BLOCKED
        data = json.loads((tmp_path / "candidate" / "env-failure.json").read_text())
        assert data["kind"] == "url" and "not ready" in data["error"]

    def test_url_token_substituted_in_command(self, tmp_path, app):
        out = tmp_path / "echo.txt"
        spec = em.EnvSpec(kind="url", url=app, out_dir=str(tmp_path))
        em.run_in_env(spec, [sys.executable, "-c", f"import sys, pathlib; pathlib.Path({str(out)!r}).write_text(sys.argv[1])", "{url}"])
        assert out.read_text() == app

    def test_missing_url_is_blocked(self, tmp_path):
        assert em.run_in_env(em.EnvSpec(kind="url", out_dir=str(tmp_path)), ["true"]) == em.EXIT_BLOCKED

    def test_unknown_kind(self):
        with pytest.raises(em.EnvError, match="unknown kind"):
            em.make_env(em.EnvSpec(kind="vm"))


class TestLogRedaction:
    def test_env_log_is_redacted_before_it_becomes_an_artifact(self, tmp_path, app, monkeypatch):
        class Leaky(em.UrlEnv):
            def logs(self):
                return "ERROR boom Authorization: Bearer abcdefghijklmnop123 password=hunter2hunter2"

        monkeypatch.setattr(em, "make_env", lambda spec: Leaky(spec))
        em.run_in_env(em.EnvSpec(kind="url", url=app, out_dir=str(tmp_path)), ["true"])
        log = (tmp_path / "candidate" / "logs" / "env.log").read_text()
        assert "abcdefghijklmnop123" not in log and "hunter2hunter2" not in log and "ERROR boom" in log


class TestDockerEnv:
    def test_start_publishes_port_waits_and_tears_down(self, tmp_path, fake_docker):
        spec = em.EnvSpec(kind="docker", image="shop:1.2", side="baseline", out_dir=str(tmp_path), env={"MODE": "test"})
        code = em.run_in_env(spec, [sys.executable, "-c", "raise SystemExit(0)"])
        calls = fake_docker.read_text().splitlines()
        run = next(c for c in calls if c.startswith("run "))
        assert code == 0 and "-p 127.0.0.1::8080" in run and "-e MODE" in run and "test" not in run.replace("shop", "") and run.endswith("shop:1.2")
        assert any(c.startswith("rm -f qa-baseline-") for c in calls)
        assert "container log line" in (tmp_path / "baseline" / "logs" / "env.log").read_text()

    def test_teardown_runs_even_when_the_command_fails(self, tmp_path, fake_docker):
        spec = em.EnvSpec(kind="docker", image="shop:1", out_dir=str(tmp_path))
        assert em.run_in_env(spec, [sys.executable, "-c", "raise SystemExit(9)"]) == 9
        assert any(c.startswith("rm -f") for c in fake_docker.read_text().splitlines())

    def test_daemon_down_is_blocked_and_nothing_is_started(self, tmp_path, fake_docker, monkeypatch):
        monkeypatch.setenv("FAKE_DOCKER_INFO_RC", "1")
        monkeypatch.setattr(em, "docker_preflight", lambda *a, **k: (False, DIND_HINT))
        spec = em.EnvSpec(kind="docker", image="shop:1", out_dir=str(tmp_path))
        assert em.run_in_env(spec, ["true"]) == em.EXIT_BLOCKED
        assert "--dind" in json.loads((tmp_path / "candidate" / "env-failure.json").read_text())["error"]
        calls = fake_docker.read_text().splitlines() if fake_docker.exists() else []
        assert not any(c.startswith("run ") or c.startswith("rm ") for c in calls)

    def test_missing_image_is_pulled_before_its_port_is_read(self, tmp_path, fake_docker):
        spec = em.EnvSpec(kind="docker", image="shop:1", out_dir=str(tmp_path))
        assert em.run_in_env(spec, ["true"]) == 0
        calls = fake_docker.read_text().splitlines()
        assert calls.index("pull shop:1") < next(i for i, c in enumerate(calls) if c.startswith("run "))

    def test_present_image_is_not_pulled(self, tmp_path, fake_docker, monkeypatch):
        monkeypatch.setenv("FAKE_IMAGE_PRESENT", "1")
        em.run_in_env(em.EnvSpec(kind="docker", image="shop:1", out_dir=str(tmp_path)), ["true"])
        assert not any(c.startswith("pull ") for c in fake_docker.read_text().splitlines())

    def test_pull_failure_is_blocked_with_the_reason(self, tmp_path, fake_docker, monkeypatch):
        monkeypatch.setenv("FAKE_PULL_FAIL", "1")
        assert em.run_in_env(em.EnvSpec(kind="docker", image="nope:1", out_dir=str(tmp_path)), ["true"]) == em.EXIT_BLOCKED
        assert "pull access denied" in json.loads((tmp_path / "candidate" / "env-failure.json").read_text())["error"]

    def test_explicit_container_port_skips_image_inspect(self, tmp_path, fake_docker):
        spec = em.EnvSpec(kind="docker", image="shop:1", container_port=3000, out_dir=str(tmp_path))
        em.run_in_env(spec, ["true"])
        calls = fake_docker.read_text().splitlines()
        assert not any("ExposedPorts" in c for c in calls) and any("127.0.0.1::3000" in c for c in calls)


class TestComposeEnv:
    def test_up_wait_and_down(self, tmp_path, fake_docker, app):
        spec = em.EnvSpec(kind="compose", compose_file="docker-compose.yml", url=app, out_dir=str(tmp_path), ready_timeout=5)
        assert em.run_in_env(spec, ["true"]) == 0
        calls = fake_docker.read_text().splitlines()
        assert any(" up -d --wait --wait-timeout 5" in c for c in calls)
        assert any(" down -v --remove-orphans" in c for c in calls)

    def test_down_runs_when_up_fails(self, tmp_path, fake_docker, app, monkeypatch):
        monkeypatch.setenv("FAKE_COMPOSE_RC", "1")
        spec = em.EnvSpec(kind="compose", compose_file="c.yml", url=app, out_dir=str(tmp_path))
        assert em.run_in_env(spec, ["true"]) == em.EXIT_BLOCKED
        assert any(" down -v" in c for c in fake_docker.read_text().splitlines())

    def test_requires_file_and_url(self, tmp_path):
        assert em.run_in_env(em.EnvSpec(kind="compose", out_dir=str(tmp_path)), ["true"]) == em.EXIT_BLOCKED


class TestWorktreeEnv:
    """The git checkout + app lifecycle needs a real repository and runs in the pod suite (qa-worktree-env)."""

    def test_requires_ref_and_start_cmd(self, tmp_path):
        with pytest.raises(em.EnvError, match="required"):
            em.WorktreeEnv(em.EnvSpec(kind="worktree", out_dir=str(tmp_path))).start()


class TestCli:
    def test_run_requires_command(self, tmp_path):
        assert em.main(["run", "--kind", "url", "--url", "http://x", "--out-dir", str(tmp_path)]) == 2

    def test_preflight_url_needs_no_daemon(self, capsys):
        assert em.main(["preflight", "--kind", "url"]) == 0
        assert json.loads(capsys.readouterr().out)["ok"] is True

    def test_preflight_docker_blocked_without_daemon(self, monkeypatch, capsys):
        monkeypatch.setattr(em, "docker_preflight", lambda *a, **k: (False, DIND_HINT))
        assert em.main(["preflight", "--kind", "docker"]) == em.EXIT_BLOCKED

    def test_cli_run_with_dashdash(self, tmp_path, app):
        assert em.main(["run", "--kind", "url", "--url", app, "--out-dir", str(tmp_path), "--", sys.executable, "-c", "raise SystemExit(4)"]) == 4

    def test_cleanup_removes_container(self, tmp_path, fake_docker):
        st = tmp_path / "s.json"
        st.write_text(json.dumps({"kind": "docker", "name": "qa-x-1"}))
        em.cleanup(str(st))
        assert "rm -f qa-x-1" in fake_docker.read_text()


# --- lifecycle, safety and contract regressions found in review ------------------------------------------------


def run_cli(args, env=None, cwd=None):
    return subprocess.run([sys.executable, "-m", "scripts.qa.env_manager", *args], capture_output=True, text=True, env=env, cwd=cwd, timeout=60)


class TestRunContract:
    def test_stale_failure_from_an_earlier_run_does_not_survive_a_successful_one(self, tmp_path, app):
        side = tmp_path / "candidate"
        side.mkdir()
        (side / "env-failure.json").write_text('{"error": "old"}')
        (side / "env-state.json").write_text("{}")
        assert em.run_in_env(em.EnvSpec(kind="url", url=app, out_dir=str(tmp_path)), ["true"]) == 0
        assert not (side / "env-failure.json").exists() and not (side / "env-state.json").exists()

    def test_missing_test_command_is_reported_not_raised(self, tmp_path, app, capsys):
        assert em.run_in_env(em.EnvSpec(kind="url", url=app, out_dir=str(tmp_path)), ["no-such-command-xyz"]) == 127
        assert "cannot run the test command" in capsys.readouterr().err

    def test_allow_mutations_is_exported_only_when_asked(self, tmp_path, app):
        out = tmp_path / "o.txt"
        probe = [sys.executable, "-c", f"import os, pathlib; pathlib.Path({str(out)!r}).write_text(os.environ.get('QA_ALLOW_MUTATIONS', 'unset'))"]
        em.run_in_env(em.EnvSpec(kind="url", url=app, out_dir=str(tmp_path)), probe)
        assert out.read_text() == "unset"
        em.run_in_env(em.EnvSpec(kind="url", url=app, out_dir=str(tmp_path), allow_mutations=True), probe)
        assert out.read_text() == "1"

    def test_platform_credentials_do_not_reach_the_test_command_but_target_credentials_do(self, tmp_path, app, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "platform-secret")
        monkeypatch.setenv("JIRA_API_TOKEN", "platform-secret")
        monkeypatch.setenv("QA_PASSWORD", "target-password")
        out = tmp_path / "o.json"
        probe = [sys.executable, "-c", f"import os, json, pathlib; pathlib.Path({str(out)!r}).write_text(json.dumps({{k: os.environ.get(k) for k in ('GH_TOKEN', 'JIRA_API_TOKEN', 'QA_PASSWORD')}}))"]
        em.run_in_env(em.EnvSpec(kind="url", url=app, out_dir=str(tmp_path)), probe)
        assert json.loads(out.read_text()) == {"GH_TOKEN": None, "JIRA_API_TOKEN": None, "QA_PASSWORD": "target-password"}

    def test_env_entries_without_equals_are_rejected(self, tmp_path):
        res = run_cli(["run", "--kind", "url", "--url", "http://127.0.0.1:1", "--env", "NOEQUALS", "--out-dir", str(tmp_path), "--", "true"])
        assert res.returncode == 2 and "KEY=VALUE" in res.stderr


class TestStartupProblemsAreBlockedNotCrashes:
    def test_helper_timeout_and_missing_binary_become_env_errors(self):
        with pytest.raises(em.EnvError, match="timed out"):
            em._sh(["sleep", "5"], timeout=0.2)
        with pytest.raises(em.EnvError, match="cannot run"):
            em._sh(["no-such-binary-xyz"])

    @pytest.mark.parametrize("spec", [
        em.EnvSpec(kind="docker", image="-v/etc:/x"), em.EnvSpec(kind="worktree", ref="--upload-pack=x", start_cmd="true"),
        em.EnvSpec(kind="docker"),
    ])
    def test_option_like_or_missing_values_are_blocked(self, tmp_path, spec, fake_docker):
        spec.out_dir = str(tmp_path)
        assert em.run_in_env(spec, ["true"]) == em.EXIT_BLOCKED
        assert (tmp_path / "candidate" / "env-failure.json").exists()

    def test_start_command_binary_missing_is_blocked_and_the_worktree_is_still_removed(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        spec = em.EnvSpec(kind="worktree", ref="HEAD", repo_dir=str(repo), start_cmd="no-such-binary-xyz", out_dir=str(tmp_path / "out"))
        assert em.run_in_env(spec, ["true"]) == em.EXIT_BLOCKED  # git worktree add fails (not a repo) -> EnvError, not a traceback
        assert "failed" in json.loads((tmp_path / "out" / "candidate" / "env-failure.json").read_text())["error"]

    def test_relative_paths_are_resolved_once(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        spec = em.EnvSpec(kind="worktree", ref="HEAD", repo_dir="repo", start_cmd="true", out_dir="rel-out")
        assert spec.side_dir.is_absolute() and em.WorktreeEnv(spec).dir.is_absolute()


class TestDockerLifecycle:
    def test_container_is_removed_when_docker_run_fails_after_creating_it(self, tmp_path, fake_docker, monkeypatch):
        monkeypatch.setenv("FAKE_RUN_FAIL", "1")
        spec = em.EnvSpec(kind="docker", image="shop:1", out_dir=str(tmp_path))
        assert em.run_in_env(spec, ["true"]) == em.EXIT_BLOCKED
        calls = fake_docker.read_text().splitlines()
        assert any(c.startswith("run ") for c in calls) and any(c.startswith("rm -f qa-candidate-") for c in calls)

    def test_secret_environment_values_are_never_in_the_docker_argv(self, tmp_path, fake_docker):
        spec = em.EnvSpec(kind="docker", image="shop:1", env={"DB_PASSWORD": "hunter2hunter2"}, out_dir=str(tmp_path))
        em.run_in_env(spec, ["true"])
        log = fake_docker.read_text()
        assert "-e DB_PASSWORD" in log and "hunter2hunter2" not in log

    def test_empty_port_mapping_is_blocked(self, tmp_path, fake_docker, monkeypatch):
        monkeypatch.setenv("FAKE_PORT_EMPTY", "1")
        assert em.run_in_env(em.EnvSpec(kind="docker", image="shop:1", out_dir=str(tmp_path)), ["true"]) == em.EXIT_BLOCKED

    def test_state_file_exists_while_running_and_is_removed_after_teardown(self, tmp_path, fake_docker):
        seen = tmp_path / "seen.json"
        probe = [sys.executable, "-c", f"import pathlib; pathlib.Path({str(seen)!r}).write_text(pathlib.Path({str(tmp_path / 'candidate' / 'env-state.json')!r}).read_text())"]
        em.run_in_env(em.EnvSpec(kind="docker", image="shop:1", out_dir=str(tmp_path)), probe)
        assert json.loads(seen.read_text())["name"].startswith("qa-candidate-") and not (tmp_path / "candidate" / "env-state.json").exists()


class TestSignals:
    def _run_and_terminate(self, tmp_path):
        proc = subprocess.Popen([sys.executable, "-m", "scripts.qa.env_manager", "run", "--kind", "docker", "--image", "shop:1", "--out-dir", str(tmp_path),
                                 "--", sys.executable, "-c", "import os, threading; print('CHILD_PID=%d' % os.getpid(), flush=True); threading.Event().wait()"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        child_pid = 0
        for line in proc.stdout:  # condition wait: the child announces itself once the environment is up
            if line.startswith("CHILD_PID="):
                child_pid = int(line.split("=")[1])
                break
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=60)
        return proc.returncode, child_pid

    @staticmethod
    def _gone(pid):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            threading.Event().wait(0.02)
        return False

    def test_sigterm_tears_the_environment_down_and_leaves_no_process_behind(self, tmp_path, fake_docker):
        for attempt in range(3):  # the signal arrives right as the command starts: the window that used to leak the child
            out = tmp_path / str(attempt)
            code, pid = self._run_and_terminate(out)
            assert code == 130 and pid and self._gone(pid), f"attempt {attempt}: exit {code}, child {pid} alive"
            assert not (out / "candidate" / "env-state.json").exists()
        assert sum(1 for c in fake_docker.read_text().splitlines() if c.startswith("rm -f qa-candidate-")) == 3


class TestStaleStateAndInputs:
    def test_leftovers_of_a_killed_run_are_cleaned_up_before_the_next_one(self, tmp_path, fake_docker, app):
        side = tmp_path / "candidate"
        side.mkdir()
        (side / "env-state.json").write_text(json.dumps({"kind": "docker", "name": "qa-candidate-dead"}))
        (side / "results.jsonl").write_text('{"case": "C1", "outcome": "fail"}\n')
        assert em.run_in_env(em.EnvSpec(kind="url", url=app, out_dir=str(tmp_path)), ["true"]) == 0
        assert "rm -f qa-candidate-dead" in fake_docker.read_text() and not (side / "results.jsonl").exists()

    def test_a_malformed_url_is_blocked_not_a_traceback(self, tmp_path):
        assert em.run_in_env(em.EnvSpec(kind="url", url="http://[x", out_dir=str(tmp_path)), ["true"]) == em.EXIT_BLOCKED
        assert "invalid url" in json.loads((tmp_path / "candidate" / "env-failure.json").read_text())["error"]

    def test_compose_and_docker_run_without_the_platforms_credentials(self, tmp_path, fake_docker, app, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "platform-secret")
        monkeypatch.setenv("NPM_TOKEN", "some-registry-token")
        em.run_in_env(em.EnvSpec(kind="compose", compose_file="c.yml", url=app, out_dir=str(tmp_path), ready_timeout=5), ["true"])
        em.run_in_env(em.EnvSpec(kind="docker", image="shop:1", out_dir=str(tmp_path / "d")), ["true"])
        started = [l for l in (fake_docker.parent / "docker.env").read_text().splitlines() if l.startswith(("run ", "compose "))]
        assert len(started) >= 3 and all("GH_TOKEN=unset NPM_TOKEN=unset" in l for l in started), started
class TestCleanup:
    def test_docker_state_is_cleaned_and_the_state_file_removed(self, tmp_path, fake_docker):
        st = tmp_path / "s.json"
        st.write_text(json.dumps({"kind": "docker", "name": "qa-x-1"}))
        em.cleanup(str(st))
        assert "rm -f qa-x-1" in fake_docker.read_text() and not st.exists()

    @pytest.mark.skipif(not Path("/proc").exists(), reason="needs /proc")
    def test_a_recycled_pid_is_not_killed(self, tmp_path):
        assert em._process_belongs_to(os.getpid(), os.getcwd()) is True
        assert em._process_belongs_to(os.getpid(), str(tmp_path / "elsewhere")) is False
        assert em._process_belongs_to(2 ** 22 + 12345, os.getcwd()) is False


class TestWaitReadyProxy:
    def test_loopback_readiness_ignores_http_proxy_settings(self, app, monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
        monkeypatch.delenv("NO_PROXY", raising=False)
        em.wait_ready(app, "/", timeout=5)


class TestBoundedAndLoudTeardown:
    def test_a_test_command_that_runs_too_long_is_killed_and_reported(self, tmp_path, app, capsys):
        spec = em.EnvSpec(kind="url", url=app, out_dir=str(tmp_path), command_timeout=0.5)
        code = em.run_in_env(spec, [sys.executable, "-c", "import time; time.sleep(60)"])
        assert code == em.EXIT_TIMEOUT
        assert "exceeded" in capsys.readouterr().err and (tmp_path / "candidate" / "logs" / "env.log").exists()

    def test_results_written_before_the_timeout_are_kept(self, tmp_path, app):
        probe = [sys.executable, "-c", "import os, time; open(os.environ['QA_OUT'] + '/results.jsonl', 'a').write('{\"case\":\"C1\",\"outcome\":\"pass\"}\\n'); time.sleep(60)"]
        em.run_in_env(em.EnvSpec(kind="url", url=app, out_dir=str(tmp_path), command_timeout=1), probe)
        assert "C1" in (tmp_path / "candidate" / "results.jsonl").read_text()

    def test_the_cli_exposes_the_timeout(self, tmp_path):
        res = run_cli(["run", "--kind", "url", "--url", "http://127.0.0.1:1", "--command-timeout", "abc", "--out-dir", str(tmp_path), "--", "true"])
        assert res.returncode == 2

    def test_a_container_that_cannot_be_removed_is_reported_with_the_manual_command(self, tmp_path, fake_docker, monkeypatch, capsys):
        monkeypatch.setenv("FAKE_RM_RC", "1")
        assert em.run_in_env(em.EnvSpec(kind="docker", image="shop:1", out_dir=str(tmp_path)), ["true"]) == 0
        err = capsys.readouterr().err
        assert "could not remove" in err and "docker rm -f qa-candidate-" in err

    def test_a_compose_project_that_cannot_be_removed_is_reported(self, tmp_path, fake_docker, app, monkeypatch, capsys):
        Path(f"{fake_docker}.downfail").touch()
        spec = em.EnvSpec(kind="compose", compose_file="c.yml", url=app, out_dir=str(tmp_path), ready_timeout=5)
        em.run_in_env(spec, ["true"])
        assert "could not remove" in capsys.readouterr().err


class TestImageArchitecture:
    """The daemon (often arm64 on a laptop node) runs the image natively or not at all: no platform is ever forced."""

    def _run(self, tmp_path):
        return em.run_in_env(em.EnvSpec(kind="docker", image="shop:1", out_dir=str(tmp_path)), ["true"])

    def test_an_image_for_another_architecture_is_blocked_before_it_is_started(self, tmp_path, fake_docker, monkeypatch):
        monkeypatch.setenv("FAKE_DAEMON_ARCH", "aarch64")
        monkeypatch.setenv("FAKE_IMAGE_ARCH", "amd64")
        assert self._run(tmp_path) == em.EXIT_BLOCKED
        error = json.loads((tmp_path / "candidate" / "env-failure.json").read_text())["error"]
        assert "amd64" in error and "arm64" in error and "imagetools" in error
        assert not any(c.startswith("run ") for c in fake_docker.read_text().splitlines())

    def test_a_matching_image_is_started_and_no_platform_is_forced(self, tmp_path, fake_docker, monkeypatch):
        monkeypatch.setenv("FAKE_DAEMON_ARCH", "aarch64")
        monkeypatch.setenv("FAKE_IMAGE_ARCH", "arm64")
        assert self._run(tmp_path) == 0
        assert not any("--platform" in c for c in fake_docker.read_text().splitlines() if c.split()[0] in ("run", "pull"))

    def test_an_image_without_a_build_for_the_daemon_is_blocked_when_the_engine_reports_nothing(self, tmp_path, fake_docker, monkeypatch):
        """With the containerd image store, inspecting for the native platform of a foreign-only image prints nothing."""
        monkeypatch.setenv("FAKE_DAEMON_ARCH", "aarch64")
        monkeypatch.setenv("FAKE_IMAGE_ARCH", "")
        assert self._run(tmp_path) == em.EXIT_BLOCKED
        assert "arm64" in json.loads((tmp_path / "candidate" / "env-failure.json").read_text())["error"]
        assert not any(c.startswith("run ") for c in fake_docker.read_text().splitlines())

    def test_unknown_architectures_are_not_a_reason_to_block(self, tmp_path, fake_docker, monkeypatch):
        monkeypatch.setenv("FAKE_DAEMON_ARCH", "riscv-something")
        assert self._run(tmp_path) == 0

    def test_a_missing_native_build_is_explained_when_the_pull_fails(self, tmp_path, fake_docker, monkeypatch):
        monkeypatch.setenv("FAKE_PULL_FAIL", "1")
        monkeypatch.setenv("FAKE_PULL_MSG", "no matching manifest for linux/arm64/v8 in the manifest list entries")
        assert self._run(tmp_path) == em.EXIT_BLOCKED
        error = json.loads((tmp_path / "candidate" / "env-failure.json").read_text())["error"]
        assert "no matching manifest" in error and "imagetools" in error
