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


def ensure_debug_mode() -> None:
    """If AI_DEV_TOOLS_DEBUG=1, clone latest code and re-exec this process."""
    if os.environ.get("AI_DEV_TOOLS_DEBUG", "0") != "1":
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

    # Set flag to 0 before re-exec so the re-invoked process skips this block
    os.environ["AI_DEV_TOOLS_DEBUG"] = "0"
    os.execv(sys.executable, [sys.executable] + sys.argv)
