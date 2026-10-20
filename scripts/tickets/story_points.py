"""Story points for a ticket, as ``{"value": float | None, "source": str, "known": bool}``.

``known`` is False when the points could not be read at all (field list unavailable, no such field on the site, a
project field without access): the ticket is then reported with that fact and is never called "unset".
"""

from __future__ import annotations

import os
import re

from scripts.common import jira_api

_FIELD_NAME = re.compile(r"^story[ -]?points?\b", re.I)
_LABEL = re.compile(r"^(?:sp|points?|story[ _-]?points?|estimate)\s*[:=/ -]\s*(\d+(?:\.\d+)?)$", re.I)


def _result(value: float | None, source: str, known: bool = True) -> dict:
    return {"value": value, "source": source, "known": known}


def jira_point_fields(config: dict) -> list[tuple[str, str]]:
    """(id, name) of the site's story-point fields. ``JIRA_STORY_POINT_FIELDS`` (comma separated names or ids) overrides the
    name match. Raises :class:`JiraError` when the field list cannot be read."""
    override = [x.strip().lower() for x in (config.get("JIRA_STORY_POINT_FIELDS") or os.environ.get("JIRA_STORY_POINT_FIELDS", "")).split(",") if x.strip()]
    found = []
    for f in jira_api.list_fields(config):
        name = f.get("name", "")
        wanted = (f["id"].lower() in override or name.lower() in override) if override else bool(_FIELD_NAME.match(name))
        if wanted:
            found.append((f["id"], name))
    return found


def _number(raw: object) -> float | None:
    if isinstance(raw, dict):  # option-type field
        raw = raw.get("value")
    try:
        return float(raw) if raw is not None and raw != "" else None
    except (TypeError, ValueError):
        return None


def jira_points(issue_fields: dict, point_fields: list[tuple[str, str]] | None) -> dict:
    """First non-null story-point field of an issue. ``point_fields`` None means the field list was unreadable."""
    if point_fields is None:
        return _result(None, "field list unreadable", known=False)
    if not point_fields:
        return _result(None, "no story-point field on this site", known=False)
    for fid, name in point_fields:
        value = _number(issue_fields.get(fid))
        if value is not None:
            return _result(value, name)
    return _result(None, "/".join(n for _, n in point_fields))


def label_points(labels: list[str]) -> float | None:
    """``sp:3``, ``points: 5``, ``story-points/8`` or ``estimate-2`` as a label."""
    for label in labels:
        m = _LABEL.match(label.strip())
        if m:
            return float(m.group(1))
    return None


def github_points(labels: list[str], project_value: float | None = None, project_known: bool = True) -> dict:
    """Points of a GitHub issue: a Projects number field when one is configured and readable, else a label."""
    if project_value:
        return _result(project_value, "project field")
    from_label = label_points(labels)
    if from_label is not None:
        return _result(from_label, "label")
    if not project_known:
        return _result(None, "project field unreadable", known=False)
    return _result(None, "labels")
