"""Debug bootstrap: clone latest ai-dev-tools when AI_DEV_TOOLS_DEBUG=1.

Called automatically from load_config(). When the flag is set the current
process re-execs itself after overwriting /app/scripts with a fresh clone, so
the new code runs without rebuilding the Docker image.

Formicary k8s tasks run scripts directly (bypassing entrypoint.sh), so this
is the only reliable way to pick up code changes via the debug flag.

Re-exec guard: after cloning, AI_DEV_TOOLS_DEBUG is set to "0" before
os.execv so the re-invoked process skips the clone and runs normally.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_URL = "https://github.com/bhatti/ai-dev-tools.git"
_APP_SCRIPTS = Path("/app/scripts")
_TMP_CLONE = Path("/tmp/ai-dev-tools-debug")
# Marker written after a successful clone so subsequent script steps in the
# same container don't clone again (formicary passes AI_DEV_TOOLS_DEBUG=1 to
# every exec in the task, but the filesystem is already up-to-date after the
# first clone).
_DONE_MARKER = Path("/tmp/.adt_bootstrap_done")


def ensure_debug_mode() -> None:
    """If AI_DEV_TOOLS_DEBUG=1, clone latest code and re-exec this process.

    Idempotent within a single container: after the first successful clone the
    marker file _DONE_MARKER is created and all subsequent calls return early.
    This prevents redundant clones when multiple script steps run in the same
    Kubernetes pod (e.g. bootstrap step + record.py both call this function).
    """
    if os.environ.get("AI_DEV_TOOLS_DEBUG", "0") != "1":
        return

    if _DONE_MARKER.exists():
        print("[debug] already bootstrapped — skipping re-clone", flush=True)
        return

    print("[debug] AI_DEV_TOOLS_DEBUG=1 — cloning latest ai-dev-tools ...", flush=True)
    shutil.rmtree(_TMP_CLONE, ignore_errors=True)

    result = subprocess.run(
        ["git", "clone", "--depth", "1", _REPO_URL, str(_TMP_CLONE)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"[debug] WARNING: clone failed — continuing with image scripts\n{result.stderr[:300]}",
              file=sys.stderr, flush=True)
        return

    # Overwrite /app/scripts with cloned version
    shutil.rmtree(_APP_SCRIPTS, ignore_errors=True)
    shutil.copytree(_TMP_CLONE / "scripts", _APP_SCRIPTS)
    shutil.rmtree(_TMP_CLONE, ignore_errors=True)
    print("[debug] /app/scripts overwritten from ai-dev-tools@main", flush=True)

    # Write marker BEFORE re-exec so the re-exec'd process (and any later
    # script step) finds it and skips the clone.
    _DONE_MARKER.touch()

    # Set flag to 0 before re-exec so the re-invoked process skips this block
    os.environ["AI_DEV_TOOLS_DEBUG"] = "0"
    os.execv(sys.executable, [sys.executable] + sys.argv)
