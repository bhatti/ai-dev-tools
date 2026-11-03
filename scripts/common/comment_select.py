# SPDX-License-Identifier: LGPL-2.1-or-later
"""Budgeted, ranked selection of PR review comments for LLM context.

The audit used to feed the first 5 comments × 300 chars per PR, which cut off exactly the
reasoning senior reviewers write. This keeps the most substantive comments within a char
budget and never drops or heavily truncates comments from ``priority_authors`` (the
reviewers whose judgment we are trying to learn).

Pure functions, no I/O. The rubber-stamp predicate is injected by the caller so this module
does not depend on ``scripts.analyze.pr_fetcher``.
"""

from __future__ import annotations

from typing import Callable, Iterable

DEFAULT_COMMENT_CAP = 1500
DEFAULT_PRIORITY_CAP = 4000
_ELLIPSIS = "…"


def _names(c: dict) -> set[str]:
    return {str(c.get(k) or "").strip().lower() for k in ("author", "author_alias")} - {""}


def _cut(body: str, cap: int) -> str:
    return body if len(body) <= cap else body[:cap] + _ELLIPSIS


def select_comments(
    comments: list[dict],
    budget_chars: int,
    priority_authors: Iterable[str] = (),
    is_trivial: Callable[[str], bool] = lambda _b: False,
    per_comment_cap: int = DEFAULT_COMMENT_CAP,
    priority_cap: int = DEFAULT_PRIORITY_CAP,
) -> tuple[list[dict], int]:
    """Return ``(selected, omitted_count)``; selected keeps the original order, bodies possibly truncated.

    Order of preference: priority-author comments (always kept, cap ``priority_cap``); then, within
    ``budget_chars``, the best comment of each distinct author (non-trivial, inline-on-code, longer first);
    then remaining non-trivial comments by the same ranking; trivial approvals only if budget is left.
    """
    prio = {p.strip().lower() for p in priority_authors if p and p.strip()}
    chosen: dict[int, str] = {}  # index -> (possibly truncated) body
    used = 0

    for i, c in enumerate(comments):
        if prio & _names(c):
            chosen[i] = _cut(c.get("body", ""), priority_cap)

    def rank(i: int) -> tuple:
        c = comments[i]
        body = c.get("body", "")
        return (is_trivial(body), not c.get("path"), -len(body), i)

    def try_add(i: int) -> None:
        nonlocal used
        body = _cut(comments[i].get("body", ""), per_comment_cap)
        if used + len(body) <= budget_chars:
            chosen[i] = body
            used += len(body)

    rest = sorted((i for i in range(len(comments)) if i not in chosen), key=rank)
    seen_authors: set[str] = set()
    for i in rest:  # pass 1: best comment per distinct author
        who = (comments[i].get("author") or "").strip().lower()
        if who not in seen_authors:
            seen_authors.add(who)
            try_add(i)
    for i in rest:  # pass 2: whatever else fits
        if i not in chosen:
            try_add(i)

    selected = [{**comments[i], "body": chosen[i]} for i in sorted(chosen)]
    return selected, len(comments) - len(selected)
