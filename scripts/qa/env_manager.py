"""Start, health-check and always tear down a test environment, then run a command inside it.

Environment kinds (simplest first):
  url       already deployed, nothing to start; only readiness is checked
  docker    run an image, publish its port on loopback (needs a reachable Docker daemon)
  compose   docker compose up --wait; the caller supplies --url because port mappings vary
  worktree  check out a git ref into a worktree, optionally build, run --start-cmd with $PORT (needs a buildable project)

``run`` exports QA_BASE_URL / QA_SIDE / QA_OUT to the wrapped command (plus QA_ALLOW_MUTATIONS=1 with
--allow-mutations), writes the environment's logs, redacted, to <out-dir>/<side>/logs/env.log and tears everything
down in a ``finally`` (also on SIGTERM/SIGINT/SIGHUP, and a second signal cannot interrupt the teardown). A startup
problem of any kind is reported as BLOCKED (exit 3) with env-failure.json, never as a test failure.

Code from the PR under test (--build-cmd, --start-cmd) runs without the platform's credentials. Run ONE wrapper per
side around everything that must share the environment (API script and UI runner in a single command).

Usage:
    python3 -m scripts.qa.env_manager run --kind url --url https://staging.example --side candidate --out-dir qa-out -- bash run-all.sh
    python3 -m scripts.qa.env_manager run --kind docker --image app:1.2 --container-port 8080 --side baseline --out-dir qa-out -- bash run-all.sh
    python3 -m scripts.qa.env_manager preflight --kind docker
    python3 -m scripts.qa.env_manager cleanup --state qa-out/baseline/env-state.json
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from scripts.common.docker_check import docker_preflight
from scripts.common.exit_codes import EXIT_BLOCKED, EXIT_TIMEOUT, EXIT_USAGE
from scripts.common.env_scrub import scrubbed_environ
from scripts.common.redact import redact

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class EnvError(RuntimeError):
    """The environment could not be started or never became ready (BLOCKED, not a test failure)."""


@dataclass
class EnvSpec:
    kind: str
    side: str = "candidate"
    out_dir: str = "qa-out"
    url: str = ""
    health_path: str = "/"
    ready_timeout: float = 120.0
    image: str = ""
    container_port: int = 0
    env: dict[str, str] = field(default_factory=dict)
    compose_file: str = ""
    ref: str = ""
    repo_dir: str = "."
    build_cmd: str = ""
    start_cmd: str = ""
    allow_mutations: bool = False
    command_timeout: float = 3600.0  # the wrapped test command; results recorded before it is killed are kept

    @property
    def side_dir(self) -> Path:
        return Path(self.out_dir).resolve() / self.side


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _not_an_option(value: str, what: str) -> None:
    """A value starting with '-' would be parsed as an option by docker/git."""
    if value.startswith("-"):
        raise EnvError(f"{what} must not start with '-': {value!r}")


def _tls_context() -> ssl.SSLContext | None:
    """QA_INSECURE_TLS=1 accepts self-signed certificates (staging with an internal CA); default verifies."""
    return ssl._create_unverified_context() if os.environ.get("QA_INSECURE_TLS") == "1" else None  # noqa: S323


def _opener(url: str) -> urllib.request.OpenerDirector:
    """Loopback readiness checks must not go through an HTTP proxy from the environment."""
    handlers: list = [urllib.request.HTTPSHandler(context=_tls_context())]
    if urlparse(url).hostname in _LOOPBACK:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def wait_ready(url: str, health_path: str = "/", timeout: float = 120.0, alive: Callable[[], bool] | None = None) -> None:
    """Poll until the URL answers with a status below 500. No fixed sleeps: bounded backoff against a deadline.

    Any status below 500 counts (an API server may answer 401/404 on ``/``); pass a real ``health_path`` when the
    root is not a meaningful probe.
    """
    target = url.rstrip("/") + "/" + health_path.lstrip("/")
    try:
        opener = _opener(url)
    except ValueError as e:
        raise EnvError(f"invalid url {url!r}: {e}") from e
    deadline = time.monotonic() + timeout
    delay, last = 0.05, "no attempt made"
    while True:
        if alive is not None and not alive():
            raise EnvError(f"process exited before {target} became ready ({last})")
        try:
            with opener.open(target, timeout=5) as resp:  # noqa: S310 - declared test URL
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        except (urllib.error.URLError, OSError, ValueError) as e:
            code, last = None, f"{type(e).__name__}: {e}"
        if code is not None:
            if code < 500:
                return
            last = f"HTTP {code}"
        if time.monotonic() + delay > deadline:
            raise EnvError(f"{target} not ready after {timeout:.0f}s (last: {last})")
        threading.Event().wait(delay)
        delay = min(delay * 2, 2.0)


def _sh(argv: list[str], timeout: float = 300, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run a helper command. A missing binary or a timeout is an EnvError (BLOCKED), not an uncaught exception."""
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as e:
        raise EnvError(f"{' '.join(argv[:3])} timed out after {timeout:.0f}s") from e
    except OSError as e:
        raise EnvError(f"cannot run {argv[0]}: {e}") from e
    if check and res.returncode != 0:
        raise EnvError(f"{' '.join(argv[:3])} failed (exit {res.returncode}): {redact((res.stderr or res.stdout).strip()[:400])}")
    return res


def _git(args: list[str]) -> subprocess.CompletedProcess:
    return _sh(["git", *args])


def _teardown(argv: list[str], what: str, manual: str, timeout: float = 300, env: dict[str, str] | None = None) -> None:
    """Run a teardown command; a failure leaves resources behind, so say what and how to remove it by hand."""
    try:
        res = _sh(argv, check=False, timeout=timeout, env=env)
        error = "" if res.returncode == 0 else f"exit {res.returncode}: {redact((res.stderr or res.stdout).strip()[:200])}"
    except EnvError as e:
        error = str(e)
    if error:
        print(f"[qa-env] WARNING: could not remove {what} ({error}); remove it manually: {manual}", file=sys.stderr, flush=True)


class _Env:
    """Common bookkeeping: the state file lets ``cleanup`` remove leftovers if the process dies mid-start."""

    def __init__(self, spec: EnvSpec) -> None:
        self.spec = spec

    def state(self) -> dict:
        return {"kind": self.spec.kind}

    def persist(self) -> None:
        self.spec.side_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.spec.side_dir / "env-state.json.tmp"
        tmp.write_text(json.dumps(self.state()), encoding="utf-8")
        tmp.replace(self.spec.side_dir / "env-state.json")

    def logs(self) -> str:
        return ""

    def stop(self) -> None:
        return None


class UrlEnv(_Env):
    def start(self) -> str:
        if not self.spec.url:
            raise EnvError("--url is required for kind=url")
        wait_ready(self.spec.url, self.spec.health_path, self.spec.ready_timeout)
        return self.spec.url


class DockerEnv(_Env):
    def __init__(self, spec: EnvSpec) -> None:
        super().__init__(spec)
        self.name = f"qa-{spec.side}-{os.urandom(4).hex()}"
        self.started = False

    def state(self) -> dict:
        return {"kind": "docker", "name": self.name}

    def _ensure_image(self) -> None:
        """A fresh daemon (e.g. a dind sidecar) has no images, so pull before inspecting."""
        if _sh(["docker", "image", "inspect", self.spec.image], check=False).returncode != 0:
            _sh(["docker", "pull", self.spec.image], timeout=900)

    def _port(self) -> int:
        if self.spec.container_port:
            return self.spec.container_port
        self._ensure_image()
        res = _sh(["docker", "image", "inspect", "--format", "{{json .Config.ExposedPorts}}", self.spec.image], check=False)
        try:
            ports = sorted(int(k.split("/")[0]) for k in json.loads(res.stdout or "null") or {})
        except (ValueError, json.JSONDecodeError):
            ports = []
        if not ports:
            raise EnvError(f"image {self.spec.image} exposes no port; pass --container-port")
        return ports[0]

    def start(self) -> str:
        if not self.spec.image:
            raise EnvError("--image is required for kind=docker")
        _not_an_option(self.spec.image, "image")
        ok, msg = docker_preflight()
        if not ok:
            raise EnvError(msg)
        port = self._port()
        argv = ["docker", "run", "-d", "--name", self.name, "-p", f"127.0.0.1::{port}"]
        for k in self.spec.env:  # `-e K` reads the value from our environment, keeping it out of the process list
            argv += ["-e", k]
        self.persist()
        self.started = True  # remove the container even when `docker run` fails half-way (e.g. a port bind error)
        _sh([*argv, self.spec.image], timeout=600, env=scrubbed_environ(self.spec.env))
        lines = _sh(["docker", "port", self.name, f"{port}/tcp"]).stdout.strip().splitlines()
        if not lines:
            raise EnvError(f"container {self.name} published no port {port}")
        url = f"http://127.0.0.1:{lines[0].rsplit(':', 1)[-1]}"
        wait_ready(url, self.spec.health_path, self.spec.ready_timeout, alive=self._running)
        return url

    def _running(self) -> bool:
        res = _sh(["docker", "inspect", "--format", "{{.State.Running}}", self.name], check=False)
        return res.returncode == 0 and res.stdout.strip() == "true"

    def logs(self) -> str:
        res = _sh(["docker", "logs", self.name], check=False) if self.started else None
        return (res.stdout + res.stderr) if res else ""

    def stop(self) -> None:
        if self.started:
            _teardown(["docker", "rm", "-f", self.name], f"container {self.name}", f"docker rm -f {self.name}")
            self.started = False


class ComposeEnv(_Env):
    def __init__(self, spec: EnvSpec) -> None:
        super().__init__(spec)
        self.project = f"qa-{spec.side}-{os.urandom(4).hex()}"
        self.started = False

    def state(self) -> dict:
        return {"kind": "compose", "project": self.project, "file": str(Path(self.spec.compose_file).resolve())}

    def _base(self) -> list[str]:
        return ["docker", "compose", "-p", self.project, "-f", self.spec.compose_file]

    def start(self) -> str:
        if not (self.spec.compose_file and self.spec.url):
            raise EnvError("--compose-file and --url are required for kind=compose")
        ok, msg = docker_preflight()
        if not ok:
            raise EnvError(msg)
        self.persist()
        self.started = True  # tear down even if `up` fails half-way
        _sh([*self._base(), "up", "-d", "--wait", "--wait-timeout", str(int(self.spec.ready_timeout))], timeout=self.spec.ready_timeout + 600,
            env=scrubbed_environ(self.spec.env))  # the compose file comes from the PR and interpolates host variables
        wait_ready(self.spec.url, self.spec.health_path, self.spec.ready_timeout)
        return self.spec.url

    def logs(self) -> str:
        res = _sh([*self._base(), "logs", "--no-color"], check=False, env=scrubbed_environ(self.spec.env)) if self.started else None
        return (res.stdout + res.stderr) if res else ""

    def stop(self) -> None:
        if self.started:
            _teardown([*self._base(), "down", "-v", "--remove-orphans"], f"compose project {self.project}",
                      f"docker compose -p {self.project} -f {self.spec.compose_file} down -v --remove-orphans", env=scrubbed_environ(self.spec.env))
            self.started = False


class WorktreeEnv(_Env):
    def __init__(self, spec: EnvSpec) -> None:
        super().__init__(spec)
        self.dir = spec.side_dir / "worktree"
        self.proc: subprocess.Popen | None = None
        self.log_path = spec.side_dir / "logs" / "app.log"
        self.created = False

    def state(self) -> dict:
        return {"kind": "worktree", "dir": str(self.dir), "repo": str(Path(self.spec.repo_dir).resolve()), "pid": self.proc.pid if self.proc else None}

    def start(self) -> str:
        s = self.spec
        if not (s.ref and s.start_cmd):
            raise EnvError("--ref and --start-cmd are required for kind=worktree")
        _not_an_option(s.ref, "ref")
        port = free_port()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        repo = str(Path(s.repo_dir).resolve())
        self.persist()
        self.created = True  # remove the worktree even if `git worktree add` fails half-way
        _git(["-C", repo, "worktree", "add", "--detach", str(self.dir), s.ref])
        env = scrubbed_environ({**s.env, "PORT": str(port), "QA_PORT": str(port)})  # PR code: no platform credentials
        try:
            with self.log_path.open("ab") as log:
                if s.build_cmd:
                    res = subprocess.run(shlex.split(s.build_cmd.replace("{port}", str(port))), cwd=self.dir, env=env, stdout=log,
                                         stderr=subprocess.STDOUT, timeout=1800)
                    if res.returncode != 0:
                        raise EnvError(f"build command failed (exit {res.returncode}); see logs/app.log")
                self.proc = subprocess.Popen(shlex.split(s.start_cmd.replace("{port}", str(port))), cwd=self.dir, env=env, stdout=log,
                                             stderr=subprocess.STDOUT, start_new_session=True)
        except subprocess.TimeoutExpired as e:
            raise EnvError("build command timed out after 1800s") from e
        except (OSError, ValueError) as e:
            raise EnvError(f"cannot run the build/start command: {e}") from e
        self.persist()  # now with the pid
        url = f"http://127.0.0.1:{port}"
        wait_ready(url, s.health_path, s.ready_timeout, alive=lambda: self.proc is not None and self.proc.poll() is None)
        return url

    def logs(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""

    def stop(self) -> None:
        if self.proc:
            for sig in (signal.SIGTERM, signal.SIGKILL):  # signal the whole group: it can outlive a leader that already exited
                try:
                    os.killpg(self.proc.pid, sig)
                except ProcessLookupError:
                    break
                if sig == signal.SIGTERM:
                    try:
                        self.proc.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            self.proc = None
        if self.created:
            repo = str(Path(self.spec.repo_dir).resolve())
            _teardown(["git", "-C", repo, "worktree", "remove", "--force", str(self.dir)], f"worktree {self.dir}",
                      f"git -C {repo} worktree remove --force {self.dir}")
            self.created = False
        if self.log_path.exists():  # the app log may hold secrets and is about to become an artifact
            self.log_path.write_text(redact(self.log_path.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")


def make_env(spec: EnvSpec) -> UrlEnv | DockerEnv | ComposeEnv | WorktreeEnv:
    kinds = {"url": UrlEnv, "docker": DockerEnv, "compose": ComposeEnv, "worktree": WorktreeEnv}
    if spec.kind not in kinds:
        raise EnvError(f"unknown kind {spec.kind!r}; choose one of {', '.join(kinds)}")
    return kinds[spec.kind](spec)


def run_in_env(spec: EnvSpec, command: list[str]) -> int:
    """Start the environment, run ``command`` inside it, always collect logs and tear down."""
    env = make_env(spec)
    side_dir = spec.side_dir
    (side_dir / "logs").mkdir(parents=True, exist_ok=True)
    leftover = side_dir / "env-state.json"
    if leftover.exists():  # a run that died before its teardown: remove what it left behind before starting again
        try:
            cleanup(str(leftover))
        except (EnvError, OSError, ValueError, KeyError) as e:
            print(f"[qa-env] could not clean up the previous run ({type(e).__name__}); continuing", file=sys.stderr, flush=True)
    for stale in ("env-failure.json", "env-state.json", "results.jsonl"):  # a previous run's verdict and results must not outlive it
        (side_dir / stale).unlink(missing_ok=True)

    def on_signal(signum, frame):  # noqa: ARG001
        raise KeyboardInterrupt

    signals = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
    previous = {s: signal.signal(s, on_signal) for s in signals}
    code = 0
    try:
        try:
            url = env.start()
        except EnvError as e:
            (side_dir / "env-failure.json").write_text(json.dumps({"side": spec.side, "kind": spec.kind, "error": str(e)}), encoding="utf-8")
            print(f"[qa-env] BLOCKED ({spec.side}, {spec.kind}): {e}", file=sys.stderr, flush=True)
            return EXIT_BLOCKED
        child_env = scrubbed_environ({"QA_BASE_URL": url, "QA_SIDE": spec.side, "QA_OUT": str(side_dir),
                                      **({"QA_ALLOW_MUTATIONS": "1"} if spec.allow_mutations else {})}, keep_qa=True)
        argv = [a.replace("{url}", url) for a in command]
        print(f"[qa-env] {spec.side} ready at {url}; running: {' '.join(shlex.quote(a) for a in argv)}", flush=True)
        proc = None
        try:
            # The test command runs in its own session, and a signal cannot land while Popen is half-done (that would
            # orphan the child): block them during the spawn, then deliver. The finally below kills the whole group.
            signal.pthread_sigmask(signal.SIG_BLOCK, set(signals))
            try:
                proc = subprocess.Popen(argv, env=child_env, start_new_session=True)
            finally:
                signal.pthread_sigmask(signal.SIG_UNBLOCK, set(signals))
            try:
                code = proc.wait(timeout=spec.command_timeout or None)
            except subprocess.TimeoutExpired:
                print(f"[qa-env] the test command exceeded {spec.command_timeout:.0f}s and was killed; "
                      "results recorded so far are kept, every case without a result is reported as not run", file=sys.stderr, flush=True)
                code = EXIT_TIMEOUT
        except OSError as e:
            print(f"[qa-env] cannot run the test command {argv[0]!r}: {e}", file=sys.stderr, flush=True)
            code = 127
        finally:
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)  # also reaps anything the command left running
                except ProcessLookupError:
                    pass
                proc.wait()
    finally:
        for s in signals:  # teardown must not be interrupted by a second signal
            signal.signal(s, signal.SIG_IGN)
        try:
            (side_dir / "logs" / "env.log").write_text(redact(env.logs()), encoding="utf-8")  # artifacts outlive the run
        finally:
            env.stop()
            (side_dir / "env-state.json").unlink(missing_ok=True)  # cleanly torn down: nothing left for `cleanup`
            for s, handler in previous.items():
                signal.signal(s, handler)
    return code


def _process_belongs_to(pid: int, directory: str) -> bool:
    """Only kill a recorded pid whose working directory is still the worktree (pids are reused)."""
    try:
        return os.path.realpath(os.readlink(f"/proc/{pid}/cwd")).startswith(os.path.realpath(directory))
    except OSError:
        return False


def cleanup(state_path: str) -> None:
    """Remove leftovers recorded by a run that died before its finally block (e.g. a killed pod)."""
    st = json.loads(Path(state_path).read_text(encoding="utf-8"))
    if st["kind"] == "docker":
        _sh(["docker", "rm", "-f", st["name"]], check=False)
    elif st["kind"] == "compose":
        _sh(["docker", "compose", "-p", st["project"], "-f", st["file"], "down", "-v", "--remove-orphans"], check=False)
    elif st["kind"] == "worktree":
        if st.get("pid") and _process_belongs_to(st["pid"], st["dir"]):
            try:
                os.killpg(st["pid"], signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        _sh(["git", "-C", st["repo"], "worktree", "remove", "--force", st["dir"]], check=False)
    Path(state_path).unlink(missing_ok=True)


def _spec_from_args(a: argparse.Namespace) -> EnvSpec:
    bad = [kv for kv in a.env if "=" not in kv]
    if bad:
        raise ValueError(f"--env expects KEY=VALUE, got {bad}")
    return EnvSpec(kind=a.kind, side=a.side, out_dir=a.out_dir, url=a.url, health_path=a.health_path, ready_timeout=a.ready_timeout,
                   image=a.image, container_port=a.container_port, env=dict(kv.split("=", 1) for kv in a.env), compose_file=a.compose_file,
                   ref=a.ref, repo_dir=a.repo_dir, build_cmd=a.build_cmd, start_cmd=a.start_cmd,
                   allow_mutations=getattr(a, "allow_mutations", False), command_timeout=getattr(a, "command_timeout", 3600.0))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="action", required=True)
    for name in ("run", "preflight"):
        sp = sub.add_parser(name)
        sp.add_argument("--kind", required=True, choices=["url", "docker", "compose", "worktree"])
        sp.add_argument("--side", default="candidate")
        sp.add_argument("--out-dir", default="qa-out")
        sp.add_argument("--url", default="")
        sp.add_argument("--health-path", default="/")
        sp.add_argument("--ready-timeout", type=float, default=120.0)
        sp.add_argument("--image", default="")
        sp.add_argument("--container-port", type=int, default=0)
        sp.add_argument("--env", action="append", default=[], metavar="K=V")
        sp.add_argument("--compose-file", default="")
        sp.add_argument("--ref", default="")
        sp.add_argument("--repo-dir", default=".")
        sp.add_argument("--build-cmd", default="")
        sp.add_argument("--start-cmd", default="")
        if name == "run":
            sp.add_argument("--command-timeout", type=float, default=3600.0, help="seconds the test command may run before it is killed (0 = no limit)")
            sp.add_argument("--allow-mutations", action="store_true", help="export QA_ALLOW_MUTATIONS=1 to the test command")
            sp.add_argument("command", nargs=argparse.REMAINDER)
    cl = sub.add_parser("cleanup")
    cl.add_argument("--state", required=True)
    a = p.parse_args(argv)
    if a.action == "cleanup":
        cleanup(a.state)
        return 0
    if a.action == "preflight":
        if a.kind in ("docker", "compose"):
            ok, msg = docker_preflight()
        else:
            ok, msg = True, f"{a.kind} needs no daemon"
        print(json.dumps({"ok": ok, "message": msg}))
        return 0 if ok else EXIT_BLOCKED
    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not command:
        print("a command is required after --", file=sys.stderr)
        return EXIT_USAGE
    try:
        spec = _spec_from_args(a)
    except ValueError as e:
        print(e, file=sys.stderr)
        return EXIT_USAGE
    try:
        return run_in_env(spec, command)
    except KeyboardInterrupt:  # SIGTERM/SIGINT/SIGHUP: the teardown already ran in run_in_env's finally
        return 130


if __name__ == "__main__":
    sys.exit(main())
