"""The one ticket shape both trackers are normalized to, and the error type adapters raise."""

from __future__ import annotations

from scripts.tickets.sections import has_plan
from scripts.tickets.signals import signals



class TicketError(RuntimeError):
    """A tracker call failed. ``kind`` is ``not_found``, ``access`` or ``failed``."""

    def __init__(self, message: str, kind: str = "failed"):
        super().__init__(message)
        self.kind = kind


def new_ticket(*, tracker: str, id: str, url: str, title: str, description: str, status: str, status_category: str,
               issue_type: str = "issue", assignee: str | None = None, labels: list[str] | None = None, parent: dict | None = None,
               is_subtask: bool = False, blocked_by: list[str] | None = None, story_points: dict, comments: list[str] | None = None,
               sprint: str | None = None, environment: str = "") -> dict:
    """A normalized ticket. ``status_category`` is ``todo``, ``in_progress`` or ``done``; ``blocked`` is derived from labels and links."""
    labels = labels or []
    blocked_by = blocked_by or []
    all_comments = [c.strip() for c in (comments or []) if c and c.strip()]
    return {
        "tracker": tracker, "id": id, "url": url, "title": title, "type": issue_type, "status": status, "status_category": status_category,
        "assignee": assignee, "labels": labels, "sprint": sprint, "parent": parent, "is_subtask": is_subtask,
        "blocked": bool(blocked_by) or any(l.lower() in ("blocked", "status: blocked", "status/blocked") for l in labels),
        "blocked_by": blocked_by, "in_flight": status_category == "in_progress",
        "story_points": story_points, "description": description, "signals": signals(description), "has_impl_plan": has_plan(description),
        "description_chars": len(description), "comments": all_comments, "comments_total": len(all_comments), "environment": environment,
        "fetch_error": "", "access_denied": False,
    }


def unreadable(tracker: str, ref: str, error: str, access_denied: bool = False) -> dict:
    """A placeholder for a ticket that could not be read: graded as UNREADABLE, never guessed at."""
    t = new_ticket(tracker=tracker, id=ref, url="", title="", description="", status="", status_category="todo",
                   story_points={"value": None, "source": "unreadable", "known": False})
    t["fetch_error"], t["access_denied"] = error, access_denied
    return t
