"""The one lane model and story-point totals behind sprint-ready and standup.

A ticket is in exactly one lane: ``pending`` (not started, or blocked), ``in_progress``, ``review`` or ``done``. The workflow's
status category decides pending / in progress / done; the status name splits review (In Review, Code Complete, QA, ...) out of
in progress, and ``Blocked`` is pending. A project whose workflow uses other names lists them once (``lanes`` in
``.ygs/readiness.yml``, or the ``SPRINT_LANES`` environment variable as JSON) and every report agrees.

Totals are computed here, once, from the tracker data; reports and the model use them as given and never add points themselves.
Sub-tasks are left out of the totals because their points live on the parent. A ticket without points is named in its lane
(also in ``done``: unpointed finished work undercounts velocity) instead of silently counting as zero.
"""

from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from typing import Iterable, Mapping, Sequence

LANES = ("pending", "in_progress", "review", "done")
OPEN_LANES = ("pending", "in_progress", "review")
LANE_LABELS = {"pending": "pending", "in_progress": "in progress", "review": "review", "done": "done"}

_REVIEW = re.compile(r"review|code complete|\bqa\b|verif|testing", re.I)
_CATEGORY = {"new": "pending", "todo": "pending", "to do": "pending", "indeterminate": "in_progress", "in_progress": "in_progress", "done": "done"}


def lane_of(status: str, category: str, overrides: Mapping[str, Sequence[str]] | None = None) -> str:
    """The lane of a status. ``category`` is the tracker's status category (Jira: new / indeterminate / done; normalized: todo /
    in_progress / done); without one the status name decides. ``overrides`` ({lane: [status names]}) win, case-insensitively."""
    name = (status or "").strip().lower()
    for lane, names in (overrides or {}).items():
        if name in {n.strip().lower() for n in names}:
            return lane
    if name == "blocked":
        return "pending"
    cat = _CATEGORY.get((category or "").strip().lower())
    if cat is None:
        cat = "done" if name in ("done", "closed", "resolved") else "in_progress" if _REVIEW.search(name) or "progress" in name else "pending"
    if cat == "done" or cat == "pending":
        return cat
    return "review" if _REVIEW.search(name) else "in_progress"


def overrides_from_env(env: Mapping[str, str] | None = None) -> dict[str, list[str]]:
    """``SPRINT_LANES`` as JSON ({"review": ["QA Ready"]}); {} when unset, ValueError when malformed (never silently ignored)."""
    raw = (env if env is not None else os.environ).get("SPRINT_LANES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"SPRINT_LANES is not valid JSON: {e}") from e
    if not isinstance(data, dict) or any(k not in LANES or not isinstance(v, list) or not all(isinstance(n, str) for n in v) for k, v in data.items()):
        raise ValueError(f"SPRINT_LANES must map lanes ({', '.join(LANES)}) to lists of status names")
    return data


def _points(item: Mapping) -> float | None:
    value = item.get("story_points")
    return float(value) if value else None  # None and 0 both mean "no estimate"


def summarize(items: Iterable[Mapping]) -> dict:
    """Totals per lane from items shaped {id, lane, story_points, is_subtask}: count, points and the ids without points."""
    lanes = {lane: {"count": 0, "points": 0.0, "unpointed": []} for lane in LANES}
    for it in items:
        if it.get("is_subtask"):
            continue
        bucket = lanes[it["lane"]]
        bucket["count"] += 1
        points = _points(it)
        if points is None:
            bucket["unpointed"].append(it["id"])
        else:
            bucket["points"] += points
    total_points = sum(b["points"] for b in lanes.values())
    open_ = [lanes[lane] for lane in OPEN_LANES]
    return {
        "lanes": lanes,
        "total": {"count": sum(b["count"] for b in lanes.values()), "points": total_points, "unpointed": sum(len(b["unpointed"]) for b in lanes.values())},
        "open": {"count": sum(b["count"] for b in open_), "points": sum(b["points"] for b in open_)},
        "share": {lane: (lanes[lane]["points"] / total_points if total_points else 0.0) for lane in LANES},
    }


def by_person(items: Iterable[Mapping]) -> dict[str, dict]:
    """``summarize`` per assignee (``unassigned`` for none), for items that also carry ``assignee``."""
    groups: dict[str, list] = defaultdict(list)
    for it in items:
        groups[it.get("assignee") or "unassigned"].append(it)
    return {person: summarize(group) for person, group in sorted(groups.items())}


def _pts(value: float) -> str:
    return f"{value:g}"


def format_line(summary: dict) -> str:
    """One line for reports: ``pending 10 pts (5) · in progress 11 pts (6) · ... · total 48 pts (23)``."""
    parts = [f"{LANE_LABELS[lane]} {_pts(summary['lanes'][lane]['points'])} pts ({summary['lanes'][lane]['count']})" for lane in LANES]
    parts.append(f"total {_pts(summary['total']['points'])} pts ({summary['total']['count']})")
    return " · ".join(parts)
