"""Read/write artifacts in /workspace/{issue_id}/ directory.

All inter-step communication goes through JSON/markdown files.
"""

import json
from pathlib import Path

from scripts.common.config import get_issue_dir


def _resolve(config: dict, issue_id: str, filename: str) -> Path:
    return get_issue_dir(config, issue_id) / filename


def write_json(config: dict, issue_id: str, filename: str, data: dict) -> Path:
    """Write JSON artifact, creating parent dirs as needed."""
    path = _resolve(config, issue_id, filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
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
