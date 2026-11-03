# SPDX-License-Identifier: LGPL-2.1-or-later
"""Resolve what a person types ("CJ", "pat", "JB", "bravo") to people in a user directory.

Matching tiers, first tier with any hit wins:
  1. exact: full display name, full nickname (accent/case-insensitive) or an id (uuid, account id)
  2. loose: first name, last name or any other name token (nickname tokens and "(nicknames)" included),
     initials of the display name or nickname
A loose match can return several people ("pat" -> every Pat); callers decide what to do with them. More than
``MAX_LOOSE`` loose matches means the input is too ambiguous to be an identity, and none are returned.
"""

from __future__ import annotations

import re
import unicodedata

MAX_LOOSE = 25
_PAREN = re.compile(r"\(.*?\)")


def normalize(text: str | None) -> list[str]:
    """Lower-case word tokens with accents removed."""
    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.findall(r"[a-z0-9]+", ascii_text.lower())


def initials(name: str | None) -> str:
    """First letters of the words outside parentheses: "Erin Bell (Belle)" -> "eb"."""
    return "".join(t[0] for t in normalize(_PAREN.sub(" ", name or "")))


def _person_keys(p: dict) -> tuple[set[str], set[str], set[str]]:
    display, nick = p.get("display_name") or "", p.get("nickname") or ""
    full = {" ".join(normalize(display)), " ".join(normalize(_PAREN.sub(" ", display))), " ".join(normalize(nick))} - {""}
    ids = {str(p.get(k)).lower() for k in ("uuid", "account_id") if p.get(k)}
    loose = set(normalize(display)) | set(normalize(nick)) | {initials(display), initials(nick), "".join(normalize(nick))}
    return full, ids, {t for t in loose if len(t) >= 2}


def match_people(requested: str, people: list[dict]) -> list[dict]:
    """People matching ``requested`` (see module docstring); [] when nothing, or too much, matches."""
    raw = (requested or "").strip()
    key = " ".join(normalize(raw))
    if not key:
        return []
    keyed = [(p, *_person_keys(p)) for p in people]
    exact = [p for p, full, ids, _ in keyed if key in full or raw.lower() in ids]
    if exact:
        return exact
    loose = [p for p, _, _, tokens in keyed if len(key) >= 2 and key.replace(" ", "") in tokens or key in tokens]
    return loose if len(loose) <= MAX_LOOSE else []
