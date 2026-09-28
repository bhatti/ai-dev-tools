"""Read/write artifacts in /workspace/{issue_id}/ directory.

All inter-step communication goes through JSON/markdown files.
"""

import json
import os
import tempfile
from pathlib import Path

from scripts.common.config import get_issue_dir


def _resolve(config: dict, issue_id: str, filename: str) -> Path:
    return get_issue_dir(config, issue_id) / filename


def _safe_write(path: Path, content: str) -> None:
    """Write content atomically via temp file + os.replace().

    In k8s pods, /workspace is a shared emptyDir. A previous task may have
    created *path* as a different UID, making direct overwrite fail with
    PermissionError.  os.replace() only requires write+execute on the
    *directory*, not ownership of the target file.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o777)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
        fd = -1
        os.replace(tmp, str(path))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json(config: dict, issue_id: str, filename: str, data: dict) -> Path:
    """Write JSON artifact, creating parent dirs as needed."""
    path = _resolve(config, issue_id, filename)
    _safe_write(path, json.dumps(data, indent=2))
    return path


def read_json(config: dict, issue_id: str, filename: str) -> dict | None:
    """Read JSON artifact. Returns None if file not found."""
    path = _resolve(config, issue_id, filename)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_text(config: dict, issue_id: str, filename: str, content: str) -> Path:
    """Write text artifact (plan.md, learnings.md, etc.)."""
    path = _resolve(config, issue_id, filename)
    _safe_write(path, content)
    return path


def read_text(config: dict, issue_id: str, filename: str) -> str | None:
    """Read text artifact. Returns None if file not found."""
    path = _resolve(config, issue_id, filename)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def write_log(config: dict, issue_id: str, step_name: str, content: str) -> Path:
    """Write to logs/ subdirectory."""
    return write_text(config, issue_id, f"logs/{step_name}.log", content)


def list_artifacts(config: dict, issue_id: str) -> list[str]:
    """List all artifacts for an issue."""
    d = get_issue_dir(config, issue_id)
    return [str(p.relative_to(d)) for p in sorted(d.rglob("*")) if p.is_file()]


def find_plan_content(issue_dir: Path) -> str | None:
    """Find plan content written by Claude — checks PLANS/*.md then plan.md directly."""
    plans_dir = issue_dir / "PLANS"
    if plans_dir.is_dir():
        plan_files = sorted(plans_dir.glob("*.md"))
        if plan_files:
            return plan_files[0].read_text(encoding="utf-8").strip() or None
    plan_md = issue_dir / "plan.md"
    if plan_md.exists():
        return plan_md.read_text(encoding="utf-8").strip() or None
    return None
