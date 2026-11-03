# SPDX-License-Identifier: LGPL-2.1-or-later
"""``--reviewers``: who was asked for, who that resolves to, and which PRs involve them.

A *scope* maps each requested name to the people it resolves to::

    {"CJ": [{"display_name": "Casey Jordan", "nickname": "cjordan", "uuid": "{..}", "account_id": "..."}],
     "pat": [<Pat Duncan>, <Pat Holloway>]}

Resolution (``resolve``) uses a user directory and ``scripts.common.person_match`` (exact name/id, then first or
last name, nickname, initials). A name that resolves to nobody is kept as a literal person, so an exact login,
nickname or id still works when no directory is available. Everything here is pure; the Bitbucket/GitHub calls
live in ``pr_fetcher``, which passes the directory in.
"""

from __future__ import annotations

import re

from scripts.common.person_match import match_people

Scope = dict[str, list[dict]]

_GH_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_BB_UUID = re.compile(r"^\{[0-9a-fA-F-]{36}\}$")
_BB_ACCOUNT_ID = re.compile(r"^(?:[0-9a-f]{24}|\d+:[0-9a-fA-F-]{36})$")


def reviewer_list(reviewers_str: str | None) -> list[str]:
    """Requested names in their original spelling, trimmed, unique case-insensitively."""
    seen: dict[str, str] = {}
    for r in (reviewers_str or "").split(","):
        name = r.strip()
        if name and name != "<no value>":
            seen.setdefault(name.lower(), name)
    return list(seen.values())


def person_ids(person: dict) -> list[str]:
    return [v for v in (person.get("display_name"), person.get("nickname"), person.get("uuid"), person.get("account_id")) if v]


def literal_scope(reviewers_str: str | None) -> Scope:
    """Each name stands for exactly itself (GitHub logins, or no directory)."""
    return {n: [{"display_name": n, "nickname": n}] for n in reviewer_list(reviewers_str)}


def resolve(reviewers_str: str | None, directory: list[dict]) -> Scope:
    scope: Scope = {}
    for name in reviewer_list(reviewers_str):
        scope[name] = match_people(name, directory) or [{"display_name": name, "nickname": name}]
    return scope


def identity_set(scope: Scope) -> set[str]:
    return {i.lower() for people in scope.values() for p in people for i in person_ids(p)}


# -- server-side queries -------------------------------------------------------------------------------------

def gh_searches(scope: Scope) -> tuple[list[str], list[str]]:
    """GitHub search strings (reviewed-by / commenter, author excluded) per login; values that are not valid
    logins are returned separately instead of being interpolated into a search query."""
    searches, skipped, seen = [], [], set()
    for people in scope.values():
        for p in people:
            login = p.get("nickname") or ""
            if login.lower() in seen:
                continue
            seen.add(login.lower())
            if not _GH_LOGIN.match(login):
                skipped.append(login)
                continue
            searches += [f"reviewed-by:{login} -author:{login}", f"commenter:{login} -author:{login}"]
    return searches, skipped


def _bbql_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def bbql(scope: Scope) -> str:
    """BBQL matching PRs where any resolved person is an assigned reviewer, by account id, uuid or nickname (the
    nickname match is case-insensitive on the server; display names are not filterable)."""
    clauses: list[str] = []
    for people in scope.values():
        for p in people:
            acct, uuid, nick = p.get("account_id") or "", p.get("uuid") or "", p.get("nickname") or p.get("display_name") or ""
            if _BB_ACCOUNT_ID.match(acct):
                clause = f"reviewers.account_id={_bbql_quote(acct)}"
            elif _BB_UUID.match(uuid):
                clause = f"reviewers.uuid={_bbql_quote(uuid)}"
            elif _BB_UUID.match(nick):
                clause = f"reviewers.uuid={_bbql_quote(nick)}"
            elif _BB_ACCOUNT_ID.match(nick):
                clause = f"reviewers.account_id={_bbql_quote(nick)}"
            elif nick:
                clause = f"reviewers.nickname={_bbql_quote(nick)}"
            else:
                continue
            if clause not in clauses:
                clauses.append(clause)
    return " OR ".join(clauses)


# -- matching PRs ----------------------------------------------------------------------------------------------

def prefilter_bb_rows(rows: list[dict], scope: Scope) -> list[dict]:
    """Cheap pre-filter of Bitbucket list rows by ``participants`` (any non-author participant: assigned reviewers
    and people who commented or approved). Rows without participant data are kept: they cannot be proven
    irrelevant and the exact post-filter decides."""
    wanted = identity_set(scope)
    if not wanted:
        return rows
    kept = []
    for rp in rows:
        parts = rp.get("participants")
        if not parts:
            kept.append(rp)
            continue
        involved = {i.lower() for p in parts if p.get("role") != "AUTHOR" for i in person_ids(p.get("user") or {})}
        if involved & wanted:
            kept.append(rp)
    return kept


def _involved(pr: dict) -> set[str]:
    """Identities of everyone who reviewed, commented, approved or was assigned (the PR author's own comments excluded)."""
    author = (pr.get("author") or "").lower()
    out: set[str] = set()
    for c in pr.get("human_comments", []):
        who = {(c.get(k) or "").lower() for k in ("author", "author_alias", "author_id")}
        if author not in who:
            out |= who
    out |= {a.lower() for a in pr.get("approvers", []) if a}
    out |= {r.lower() for r in pr.get("reviewers", []) if r}
    out |= {x.lower() for g in pr.get("participant_groups", []) for x in g if x}
    return out - {""}


def filter_prs(prs: list[dict], scope: Scope) -> list[dict]:
    """PRs involving at least one requested person; sets ``matched_reviewers`` (the requested names that hit)."""
    if not scope:
        return prs
    by_name = {name: {i.lower() for p in people for i in person_ids(p)} for name, people in scope.items()}
    kept = []
    for pr in prs:
        involved = _involved(pr)
        hit = [name for name, ids in by_name.items() if ids & involved]
        if hit:
            pr["matched_reviewers"] = hit
            kept.append(pr)
    print(f"[pr-fetch] reviewers filter: {len(prs)} → {len(kept)} PRs ({', '.join(scope)})", flush=True)
    return kept


def identities(prs: list[dict], scope: Scope) -> dict[str, list[str]]:
    """Requested name -> the display names and nicknames of the people it resolved to that these PRs actually show
    (ids are used for matching only). Names that match no one are absent. This ties the model's output, which
    carries display names, back to a request."""
    involved = set().union(*(_involved(pr) for pr in prs)) if prs else set()
    out: dict[str, list[str]] = {}
    for name, people in scope.items():
        found: dict[str, str] = {}
        for p in people:
            if {i.lower() for i in person_ids(p)} & involved:
                for i in (p.get("display_name"), p.get("nickname")):
                    if i:
                        found.setdefault(i.lower(), i)
        if found:
            out[name] = list(found.values())
    return out
