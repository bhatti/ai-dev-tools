"""Idempotency helpers — skip a step if it already completed successfully."""

import json
import sys
from pathlib import Path


def check_done(result_path: Path, extra_output: str | None = None) -> None:
    """Exit 0 if result_path exists and contains status==DONE.

    Args:
        result_path: path to the step's result JSON file.
        extra_output: optional string to print before exiting (e.g. emit a value
                      that a downstream step reads from stdout).
    """
    if not result_path.exists():
        return
    try:
        data = json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if data.get("status") == "DONE":
        print(f"[idempotency] {result_path.name} already DONE — skipping", flush=True)
        if extra_output:
            print(extra_output, flush=True)
        sys.exit(0)
