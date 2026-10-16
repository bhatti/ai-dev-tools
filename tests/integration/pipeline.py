"""Helpers for integration tests: run the real workflow entry points (`python -m scripts...`) in a subprocess, the way
Formicary runs them, against the fake `gh` executable and the fake Bitbucket server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def base_env(tmp_path: Path, tracker: str, extra: dict | None = None) -> dict:
    """Environment of a workflow task. PATH starts with the fake `gh` directory when one is installed (monkeypatched)."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "BITBUCKET_API_BASE", "TMPDIR", "LANG")}
    env.update({"PYTHONPATH": str(ROOT), "WORKSPACE_DIR": str(tmp_path / "workspace"), "AI_DEV_TOOLS_DEBUG": "0"})
    if tracker == "github":
        env.update({"DEFAULT_TRACKER": "github", "GH_ORG": "acme", "GH_REPO": "widgets", "GH_TOKEN": "x"})
    else:
        env.update({"DEFAULT_TRACKER": "jira", "BITBUCKET_WORKSPACE": "ws", "BITBUCKET_REPO": "repo",
                    "BITBUCKET_USERNAME": "u", "BITBUCKET_TOKEN": "t"})
    env.update(extra or {})
    (tmp_path / "workspace" / "reports").mkdir(parents=True, exist_ok=True)
    return env


def run_module(module: str, env: dict, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", module, *args], capture_output=True, text=True, env=env, cwd=ROOT, timeout=timeout)


def workspace(env: dict) -> Path:
    return Path(env["WORKSPACE_DIR"])


def metrics_rows(markdown: str) -> dict[str, list[str]]:
    """Rows of the single-PR metrics table, keyed by metric name."""
    rows: dict[str, list[str]] = {}
    in_table = False
    for line in markdown.splitlines():
        if line.startswith("| Metric | Value | Benchmark | Signal | Description |"):
            in_table = True
            continue
        if in_table:
            if not line.startswith("|"):
                break
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not set(cells[0]) <= set("-: "):
                rows[cells[0]] = cells
    return rows


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
