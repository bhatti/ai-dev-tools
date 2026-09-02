"""GitHub issue resolution utilities shared by gh/query_issues.py and gh/analyze_issues.py.

Provides issue-number extraction from free-form text and a unified resolve function
so both query and analyze scripts share the same dispatch logic.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

from scripts.common.shell import run_cmd as _run

_GH_NUMBER_RE = re.compile(r"#?(\d+)")
_GH_HASH_NUMBER_RE = re.compile(r"#(\d+)")  # requires # prefix — used for prose query detection
_GH_URL_RE = re.compile(r"github\.com/[^/]+/[^/]+/issues/(\d+)", re.IGNORECASE)


def extract_github_numbers(text: str) -> list[str]:
    """Extract GitHub issue numbers from free-form text (prose, comma lists, URLs).

    Returns a deduplicated list of number strings, URL-sourced entries first.
    Handles prose like "give tldr for issue #42" via finditer on the full text.
    """
    found: list[str] = []
    seen: set[str] = set()
    for m in _GH_URL_RE.finditer(text):
        n = m.group(1)
        if n not in seen:
            found.append(n)
            seen.add(n)
    for m in _GH_NUMBER_RE.finditer(text):
        n = m.group(1)
        if n not in seen:
            found.append(n)
            seen.add(n)
    return found


def fetch_issues_by_numbers(config: dict, numbers: list[str]) -> list[dict]:
    """Fetch GitHub issues by number. Silently skips issues that cannot be retrieved."""
    org = config["GH_ORG"]
    repo = config["GH_REPO"]
    issues = []
    for num in numbers:
        try:
            result = _run([
                "gh", "issue", "view", num,
                "--repo", f"{org}/{repo}",
                "--json", "number,title,url,labels,assignees,state,body",
            ])
        except subprocess.CalledProcessError:
            print(f"[gh] warning: issue #{num} not found or no access", flush=True)
            continue
        if result.stdout.strip():
            try:
                issues.append(json.loads(result.stdout))
            except json.JSONDecodeError:
                print(f"[gh] warning: could not parse issue #{num}", flush=True)
        else:
            print(f"[gh] warning: issue #{num} not found or no access", flush=True)
    return issues


def resolve_github_issues(
    config: dict,
    query: str | None = None,
    issues_arg: str | None = None,
    label: str | None = None,
    max_results: int = 20,
    search_fn=None,
) -> list[dict]:
    """Resolve GitHub issues from a query or explicit number list.

    Resolution order:
    1. If ``issues_arg`` is given, extract numbers and fetch each directly.
    2. If ``query`` contains embedded issue numbers or URLs, fetch them directly.
    3. Otherwise call ``search_fn(config, query, label, max_results)`` for a text search.

    ``search_fn`` is injected to avoid circular imports with query_issues.py.
    """
    if issues_arg:
        numbers = extract_github_numbers(issues_arg)
        if numbers:
            return fetch_issues_by_numbers(config, numbers)

    if query:
        # Require # prefix for prose queries to avoid matching random numbers like "last 3 days"
        inline_numbers = [m.group(1) for m in _GH_HASH_NUMBER_RE.finditer(query)]
        url_numbers = [m.group(1) for m in _GH_URL_RE.finditer(query)]
        inline = list(dict.fromkeys(url_numbers + inline_numbers))  # URL-sourced first, deduped
        if inline:
            print(f"[gh] found issue number(s) in text: {inline}", flush=True)
            return fetch_issues_by_numbers(config, inline)

    if search_fn and query:
        return search_fn(config, query, label, max_results)

    return []
