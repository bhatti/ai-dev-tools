"""QA verdict policy that is independent of rendering: failure kinds, message tags, the machine-readable contract, exit codes.

A failure is not always the product's fault. ``failure_kind`` separates:
  product      the candidate (or baseline) behaved wrongly: counts against the change
  baseline     the baseline was not ready or lacked the feature: says nothing about the candidate (baseline side only)
  environment  connectivity, gateway, rate limit or cluster trouble: rerun or fix the environment
  unverified   the repro harness itself broke: nothing could be concluded
The patterns are defaults; a project overrides them per kind with ``--failure-patterns file.json``
({"environment": ["regex", ...], ...}); a kind present in the file replaces that kind's defaults.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

KINDS = ("unverified", "baseline", "environment", "product")  # first match wins, in this order
MAX_PROBLEMS = 80
_CHANNEL = {"API": "http", "UI": "ui", "CLI": "cli", "UNIT": "unit", "MANUAL": "manual"}
_READINESS_RANK = {"Ready": 0, "Ready with concerns": 1, "Not ready": 2, "Blocked": 3}
_FAIL_ON = {"concerns": 1, "not-ready": 2, "blocked": 3}

DEFAULT_PATTERNS: dict[str, list[str]] = {
    "unverified": [r"cannot evaluate", r"\bharness\b", r"traceback \(most recent call last\)"],
    "baseline": [r"timed? ?out", r"baseline lacks", r"baseline (?:is )?not ready", r"not (?:yet )?deployed"],
    "environment": [r"ECONNREFUSED", r"ECONNRESET", r"connection (?:refused|reset)", r"\b50[234]\b", r"bad gateway", r"service unavailable",
                    r"rate.?limit", r"cluster not ready", r"name or service not known", r"no route to host"],
}
_PREFIX = {"baseline": "[Baseline not ready: not a defect of the candidate] ", "environment": "[Environment / infrastructure: rerun or fix the environment] ",
           "unverified": "[Could not verify: the repro harness or its environment broke] "}


def load_patterns(path: str) -> dict[str, list[str]] | None:
    """Read ``--failure-patterns``; an empty path means defaults, a malformed file or regex is an error."""
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"cannot read failure patterns {path!r}: {e}") from e
    if not isinstance(data, dict) or not all(isinstance(v, list) for v in data.values()):
        raise ValueError(f"failure patterns {path!r} must map a kind to a list of regexes")
    for kind, rxs in data.items():
        for rx in rxs:
            try:
                re.compile(rx)
            except re.error as e:
                raise ValueError(f"failure patterns {path!r}: bad regex for {kind}: {rx!r}: {e}") from e
    return data


def failure_kind(message: str, side: str = "candidate", patterns: dict[str, list[str]] | None = None) -> str:
    """Classify one failure message. ``baseline`` can only be the answer for the baseline side."""
    merged = {**DEFAULT_PATTERNS, **(patterns or {})}
    for kind in KINDS[:-1]:
        if kind == "baseline" and side != "baseline":
            continue
        if any(re.search(rx, message or "", re.I) for rx in merged.get(kind, [])):
            return kind
    return "product"


def tag(message: str, kind: str) -> str:
    """Prefix a non-product failure so a reader does not blame the change for it."""
    return _PREFIX.get(kind, "") + message


def exit_code(readiness: str, fail_on: str = "") -> int:
    """0 unless the readiness reaches the ``--fail-on`` threshold (default: never fail the process)."""
    if not fail_on:
        return 0
    if fail_on not in _FAIL_ON:
        raise ValueError(f"--fail-on must be one of {', '.join(_FAIL_ON)}")
    return 1 if _READINESS_RANK[readiness] >= _FAIL_ON[fail_on] else 0


def exercised(plan: dict, outcomes: dict[str, dict], side: str) -> list[dict]:
    """One entry per case that ran or was blocked, with the channel it used and the file that proves it."""
    types = {c["id"]: c.get("type", "") for c in plan.get("cases", []) if c.get("id")}
    out = []
    for cid, t in types.items():
        o = outcomes.get(cid)
        if not o or o["outcome"] == "not-run":
            continue
        ev = o.get("evidence") or {}
        out.append({"id": cid, "side": side, "channel": _CHANNEL.get(t, "manual"), "status": o["outcome"],
                    "proof": ev.get("screenshot") or ev.get("evidence") or ""})
    return out


def problems(assessment: dict) -> list[dict]:
    """The reasons behind a verdict as ``{code, message}`` (blocker > problem > concern), at most ``MAX_PROBLEMS``."""
    items = [{"code": code, "message": m} for code, key in (("blocker", "blockers"), ("problem", "problems"), ("concern", "concerns"))
             for m in assessment.get(key, [])]
    return items[:MAX_PROBLEMS]
