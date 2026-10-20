"""Optional per-project readiness settings, read from ``.ygs/readiness.yml`` (every key optional)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PATH = Path(".ygs/readiness.yml")
DEFAULT_SCALE = (1, 2, 3, 5, 8, 13)
DEFAULT_NO_ENRICH = ("In Review", "Done")


@dataclass(frozen=True)
class ReadinessConfig:
    points_scale: tuple[float, ...] = DEFAULT_SCALE
    variants: tuple[dict, ...] = ()
    no_enrich_statuses: tuple[str, ...] = DEFAULT_NO_ENRICH


def default_path() -> Path:
    """``.ygs/readiness.yml`` in the working directory, else in the checked-out repository (CODEBASE_DIR: a job runs in its own
    workspace, not in the repository)."""
    codebase = os.environ.get("CODEBASE_DIR", "")
    if not DEFAULT_PATH.exists() and codebase and (Path(codebase) / DEFAULT_PATH).exists():
        return Path(codebase) / DEFAULT_PATH
    return DEFAULT_PATH


def load_config(path: str | Path | None = None) -> ReadinessConfig:
    """The project's settings; defaults when the file is absent. A malformed file raises ValueError (never silently ignored)."""
    p = Path(path) if path else default_path()
    if not p.exists():
        if path:
            raise ValueError(f"readiness config {p} does not exist")
        return ReadinessConfig()
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise ValueError(f"cannot read readiness config {p}: {e}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"readiness config {p} must be a mapping")
    scale, variants, skip = raw.get("points_scale", DEFAULT_SCALE), raw.get("variants", []), raw.get("no_enrich_statuses", DEFAULT_NO_ENRICH)
    if not (isinstance(scale, (list, tuple)) and scale and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in scale)):
        raise ValueError("points_scale must be a non-empty list of numbers")
    if not (isinstance(variants, list) and all(isinstance(v, dict) and v.get("name") for v in variants)):
        raise ValueError("variants must be a list of {name, description}")
    if not (isinstance(skip, (list, tuple)) and all(isinstance(x, str) for x in skip)):
        raise ValueError("no_enrich_statuses must be a list of status names")
    return ReadinessConfig(tuple(float(x) for x in scale), tuple(variants), tuple(skip))


def no_enrich_reason(ticket: dict, cfg: ReadinessConfig) -> str:
    """Why a ticket must not be rewritten by an agent ('' when it may be): finished, or in one of the configured statuses
    (matched against the tracker status and, for trackers without workflow states, the labels)."""
    if ticket["status_category"] == "done":
        return f"the ticket is {ticket['status'] or 'closed'}"
    names = {ticket["status"].lower(), *(label.lower() for label in ticket["labels"])}
    return next((s for s in cfg.no_enrich_statuses if s.lower() in names), "")
