"""Is a Docker daemon reachable? Shared by the skill runner (fail fast) and the QA environment manager."""

from __future__ import annotations

import os
import subprocess
import threading
import time

CLI_MISSING = "docker CLI not found in this environment"
CLI_NOT_EXECUTABLE = "docker CLI is not executable by this user"
DIND_HINT = ("Cannot reach the Docker daemon. In Formicary re-run the skill with --dind (adds a docker:dind sidecar); "
             "locally start Docker.")


def docker_preflight(timeout: float = 30.0, host: str | None = None) -> tuple[bool, str]:
    """Poll ``docker info`` until it answers or ``timeout`` passes; a dind sidecar needs a moment to come up.

    ``host`` is the DOCKER_HOST to probe (default: the current environment). Returns (ok, message). A missing CLI
    is reported immediately; an unreachable daemon returns ``DIND_HINT``.
    """
    env = {**os.environ, "DOCKER_HOST": host} if host else None
    deadline = time.monotonic() + timeout
    delay = 0.1
    while True:
        try:
            res = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, env=env,
                                 timeout=max(1.0, min(15.0, deadline - time.monotonic())))
        except FileNotFoundError:
            return False, CLI_MISSING
        except PermissionError:
            return False, CLI_NOT_EXECUTABLE
        except subprocess.TimeoutExpired:
            res = None
        if res is not None and res.returncode == 0:
            return True, f"docker daemon {res.stdout.strip()}"
        if time.monotonic() + delay > deadline:
            return False, DIND_HINT
        threading.Event().wait(delay)
        delay = min(delay * 2, 2.0)
