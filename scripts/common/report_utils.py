"""Shared report file-writing utilities."""
from __future__ import annotations

from pathlib import Path


def write_report(path: Path, content: str, encoding: str = "utf-8") -> bool:
    """Write content to path only if the file doesn't already have content.

    Prevents a later pipeline stage from clobbering a report already written by
    an earlier stage in the same job.  Returns True if written, False if skipped.
    """
    if path.exists() and path.stat().st_size > 0:
        print(f"[report] skipped (exists): {path}", flush=True)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding=encoding)
    return True
