"""One backlog told twice: the same tickets as a Jira project and as GitHub issues, with the same expected facts.

``load_jira(fake)`` and ``load_github(fake)`` register them; ``EXPECTED`` is what the normalized tickets must say on
either tracker (id differs: PROJ-n vs #n). Used by the fetch parity test and the pipeline tests.
"""

from __future__ import annotations

FULL = """## Why
Customers lose their draft when the session times out.

## Acceptance Criteria
- Given a draft older than 30 minutes, when the session expires, then the draft is restored after login. Test: make test-restore
- Given a draft, when the user logs out, then the draft is kept. Test: make test-logout
- Given a corrupt draft, when restoring, then an error toast is shown. Test: make test-corrupt

## Out of scope
Offline editing.

## Definition of Done
Unit and integration tests pass.

## Implementation Plan
Restore the draft in the session middleware.
"""

SPEC_UNDER_OTHER_HEADINGS = """## Why
Search is slow for large teams.

## Outcome
Results for 10k users appear in under 2 seconds.

## Verification
Run the load script and compare p95 before and after.
"""

PROSE = "Make the settings page nicer."

TICKETS = [  # number, title, body, points, labels, in_progress
    (1, "Restore drafts after timeout", FULL, 3, [], False),
    (2, "Speed up user search", SPEC_UNDER_OTHER_HEADINGS, 5, [], True),
    (3, "Polish settings", PROSE, None, [], False),
    (4, "Blocked export", "Export is waiting on the new storage service.", None, ["blocked"], False),
]

EXPECTED = {
    1: {"title": "Restore drafts after timeout", "points": 3.0, "blocked": False, "in_flight": False, "has_impl_plan": True, "gwt": 3, "behavior": True},
    2: {"title": "Speed up user search", "points": 5.0, "blocked": False, "in_flight": True, "has_impl_plan": False, "gwt": 0, "behavior": True},
    3: {"title": "Polish settings", "points": None, "blocked": False, "in_flight": False, "has_impl_plan": False, "gwt": 0, "behavior": False},
    4: {"title": "Blocked export", "points": None, "blocked": True, "in_flight": False, "has_impl_plan": False, "gwt": 0, "behavior": False},
}


def load_jira(jira, sprint: str = "Sprint 1") -> None:
    for n, title, body, points, labels, in_progress in TICKETS:
        jira.add_issue(f"PROJ-{n}", title, description=body, points=points, labels=labels,
                       status="In Progress" if in_progress else "To Do", category="indeterminate" if in_progress else "new")
    jira.add_search(sprint, [f"PROJ-{t[0]}" for t in TICKETS])


def load_github(gh, milestone: str = "Sprint 1") -> None:
    for n, title, body, points, labels, in_progress in TICKETS:
        extra = ([f"sp:{int(points)}"] if points else []) + (["in progress"] if in_progress else [])
        gh.add_issue(n, title, body=body, labels=[*labels, *extra], milestone=milestone)
