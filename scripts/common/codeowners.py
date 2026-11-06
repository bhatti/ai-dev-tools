# SPDX-License-Identifier: LGPL-2.1-or-later
"""CODEOWNERS parsing and matching (gitignore-style patterns, last matching rule wins).

Used by the merge-queue scope router and the PR audit's changelog. GitHub reads ``.github/CODEOWNERS``, then the root
file, then ``docs/``; Bitbucket Cloud has no native CODEOWNERS, but teams keep the same file in the repo (also looked up
under ``.bitbucket/`` and ``.gitlab/``). Ownership is read from the checkout, so it reflects that commit, not merge time.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS", ".bitbucket/CODEOWNERS", ".gitlab/CODEOWNERS")
Rule = tuple[str, list[str]]


def load(repo_dir: str | None) -> list[Rule]:
    """Rules of the first CODEOWNERS file found, in file order (duplicates kept: order is the precedence)."""
    if not repo_dir:
        return []
    for rel in LOCATIONS:
        path = Path(repo_dir) / rel
        if path.is_file():
            rules: list[Rule] = []
            for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.split(" #")[0].strip()
                if not line or line.startswith("#"):
                    continue
                pattern, *owners = line.split()
                rules.append((pattern, owners))
            return rules
    return []


@lru_cache(maxsize=2048)
def _regex(pattern: str) -> re.Pattern:
    dir_only = pattern.endswith("/")
    body = pattern.strip("/")
    anchored = pattern.startswith("/") or "/" in body
    out, i = [], 0
    while i < len(body):
        if body.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif body.startswith("/**", i) and i + 3 == len(body):
            out.append("/.*")
            i += 3
        elif body.startswith("**", i):
            out.append(".*")
            i += 2
        elif body[i] == "*":
            out.append("[^/]*")
            i += 1
        elif body[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(body[i]))
            i += 1
    prefix = "^" if anchored else "^(?:.*/)?"
    suffix = "/.*$" if dir_only else "(?:/.*)?$"
    return re.compile(prefix + "".join(out) + suffix)


def owners_for(path: str, rules: list[Rule]) -> list[str]:
    """Owners of ``path`` (last matching rule wins; a rule with no owners clears ownership)."""
    owners: list[str] = []
    for pattern, rule_owners in rules:
        if _regex(pattern).match(path):
            owners = rule_owners
    return owners


def team_name(owner: str) -> str:
    """``@org/team`` -> ``team``; ``@user`` -> ``user``; e-mail addresses unchanged."""
    return owner.lstrip("@").rsplit("/", 1)[-1] if not re.search(r"@[\w-]+\.", owner) else owner
