"""Story points for the standup: each issue carries a lane and points, and the signals carry per-lane totals.

The lane model and the arithmetic live in ``scripts.common.sprint_load`` (the same one sprint-ready reports with); this module only
shapes standup issues for it, so the board table, the per-person lines and the sprint-ready report always agree.
"""

from __future__ import annotations

from scripts.common import sprint_load
from scripts.tickets import story_points


POINTS_UNKNOWN = "Story points could not be read from the tracker: write \"points unavailable\" and show issue counts only, never point totals."


def jira_point_fields(config: dict) -> list[tuple[str, str]] | None:
    """The site's story-point fields, or None when they cannot be read (the standup then says points are unknown, never zero)."""
    try:
        return story_points.jira_point_fields(config)
    except Exception as e:  # any failure here must not stop the standup
        print(f"[standup] story points unavailable: {e}", flush=True)
        return None


def jira_fields(point_fields: list[tuple[str, str]] | None) -> str:
    """Extra Jira field ids to request, comma-prefixed."""
    return "".join(f",{fid}" for fid, _ in (point_fields or [])) + ",issuetype"


def jira_lane_and_points(fields: dict, point_fields: list[tuple[str, str]] | None, overrides: dict | None = None) -> dict:
    """{lane, story_points, points_known, is_subtask} of a raw Jira issue's ``fields``."""
    status = fields.get("status") or {}
    category = (status.get("statusCategory") or {}).get("key", "")
    points = story_points.jira_points(fields, point_fields)
    return {
        "lane": sprint_load.lane_of(status.get("name", ""), category, overrides),
        "story_points": points["value"],
        "points_known": points["known"],
        "is_subtask": bool((fields.get("issuetype") or {}).get("subtask")),
    }


def github_lane_and_points(labels: list[str], closed: bool, overrides: dict | None = None) -> dict:
    """GitHub has no workflow: closed is done, a review label is review, an in-progress label is in progress, else pending."""
    status = "done" if closed else next((l for l in labels if sprint_load.lane_of(l, "", overrides) in ("review", "in_progress")), "open")
    points = story_points.github_points(labels)
    return {
        "lane": sprint_load.lane_of(status, "done" if closed else "", overrides),
        "story_points": points["value"],
        "points_known": True,
        "is_subtask": False,
    }


def points_known(issues: list[dict]) -> bool:
    """False when any issue's points could not be read from the tracker (the standup then shows counts only)."""
    return all(i["points_known"] for i in issues)


def _items(signals: dict) -> list[dict]:
    """Open and finished issues as ``sprint_load`` items; signals written before lanes existed fall back to the status name (points then unknown)."""
    issues = [*signals.get("issues", []), *signals.get("done_issues", [])]
    return [{"id": i.get("key", ""), "lane": i.get("lane") or sprint_load.lane_of(i.get("status", ""), ""),
             "story_points": i.get("story_points"), "is_subtask": bool(i.get("is_subtask")), "assignee": i.get("assignee", "unassigned"),
             "board_id": i.get("board_id")} for i in issues]


def _cell(bucket: dict) -> str:
    return f"{bucket['points']:g} pts ({bucket['count']})"


def _days_left(end_date: str, today) -> str:
    from datetime import datetime
    try:
        return str((datetime.fromisoformat(end_date.replace("Z", "+00:00")).date() - today).days)
    except (ValueError, AttributeError):
        return "?"


def board_rows(signals: dict, today) -> list[dict]:
    """One row per sprint: {board, sprint, cells: {lane: "11 pts (6)"}, total, days_left}.

    Issues are attributed to the sprint of their ``board_id``; an issue without one belongs to the only sprint, and to none when there
    are several (it still counts in the per-person table). A tracker without sprints (GitHub) gets one row for the whole scope."""
    items = _items(signals)
    sprints = signals.get("all_sprints") or ([{"board_id": None, "board": signals.get("tracker", "—"), "name": "Current scope", "end_date": ""}] if items else [])
    rows = []
    for s in sprints:
        mine = [i for i in items if i["board_id"] == s.get("board_id") or (len(sprints) == 1 and i["board_id"] is None)]
        summary = sprint_load.summarize(mine)
        rows.append({
            "board": s.get("board", "—"), "sprint": s.get("name", "—"),
            "cells": {lane: _cell(summary["lanes"][lane]) for lane in sprint_load.LANES},
            "total": _cell(summary["total"]),
            "days_left": _days_left(s.get("end_date", ""), today),
        })
    return rows


def people_rows(signals: dict) -> list[dict]:
    """One row per person: {person, cells: {lane: "5 pts (2)"}, total}, the busiest (open points) first."""
    people = sprint_load.by_person(_items(signals))
    rows = [{"person": p, "cells": {lane: _cell(s["lanes"][lane]) for lane in sprint_load.LANES}, "total": _cell(s["total"]),
             "open_points": s["open"]["points"]} for p, s in people.items()]
    return sorted(rows, key=lambda r: (-r["open_points"], r["person"]))


def unpointed_note(signals: dict) -> str:
    """'' when every counted issue has points, else the sentence that says totals undercount (or points are unreadable)."""
    if signals.get("points_known") is False:
        return POINTS_UNKNOWN
    n = sprint_load.summarize(_items(signals))["total"]["unpointed"]
    return f"{n} issue(s) have no story points and count as 0 pts." if n else ""


def prompt_block(signals: dict) -> str:
    """Story-point totals as computed lines for the synthesis prompt; the model quotes them and never adds points itself."""
    items = _items(signals)
    if not items:
        return "no issues"
    if signals.get("points_known") is False:
        return POINTS_UNKNOWN
    lines = [f"sprint: {sprint_load.format_line(sprint_load.summarize(items))}"]
    for person, summary in sprint_load.by_person(items).items():
        lines.append(f"{person}: {sprint_load.format_line(summary)}")
    note = unpointed_note(signals)
    return "\n".join(lines + ([note] if note else []))
