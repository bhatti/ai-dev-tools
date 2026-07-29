"""Shared __main__ entry-point wrapper for standup scripts.

Usage:
    from scripts.common.entrypoint import run_main
    if __name__ == "__main__":
        run_main(main, "gather_result.json")

Catches any unhandled exception from main(), prints it with a traceback,
writes {"status": "ERROR", "reason": "..."} to the named result file, and
exits 1.  sys.exit() (BaseException) propagates normally without being caught.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


def run_main(fn, result_filename: str) -> None:
    """Run fn(); on unhandled Exception write an ERROR artifact and exit 1."""
    try:
        fn()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        try:
            from scripts.common.config import load_config, get_workspace_dir
            workspace_dir: Path = get_workspace_dir(load_config(required=[]))
            workspace_dir.mkdir(parents=True, exist_ok=True)
            (workspace_dir / result_filename).write_text(
                json.dumps({"status": "ERROR", "reason": str(e)})
            )
        except Exception:
            pass
        sys.exit(1)
