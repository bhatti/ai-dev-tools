"""Tracker-independent text signals about a ticket description (hints for the grader, never verdicts)."""

from __future__ import annotations

import re

# Specs often live under other names; text under these headings counts as behavior or verification evidence.
SPEC_HEADERS = ("outcome", "requirements?", "expected(?: behavior| result)?", "verification", "tests?", "test plan", "done when",
                "definition of done", "steps to reproduce", "success criteria")
_SPEC = re.compile(rf"^#*\s*({'|'.join(SPEC_HEADERS)})\b", re.M | re.I)
_AC = re.compile(r"^#*\s*(acceptance criteria|acs?)\b", re.M | re.I)
_WHY = re.compile(r"^#*\s*(why|background|problem|context|motivation)\b", re.M | re.I)
_SCOPE = re.compile(r"^#*\s*(out of scope|non-goals?|in scope|scope)\b", re.M | re.I)
_DOD = re.compile(r"definition of done|\bdone when\b|\btest plan\b|\bverification\b", re.I)


def signals(text: str) -> dict:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return {
        "headings": [ln.lstrip("#").strip() for ln in lines if ln.startswith("#")],
        "ac_header": bool(_AC.search(text or "")),
        "spec_headers": sorted({m.group(1).lower() for m in _SPEC.finditer(text or "")}),
        "given_when_then_lines": sum(1 for ln in lines if re.match(r"[-*]?\s*(given|when|then)\b", ln, re.I)),
        "test_lines": sum(1 for ln in lines if re.match(r"[-*]?\s*test\s*:", ln, re.I)),
        "bullet_count": sum(1 for ln in lines if ln.startswith(("- ", "* "))),
        "has_why_header": bool(_WHY.search(text or "")),
        "has_scope_header": bool(_SCOPE.search(text or "")),
        "has_dod": bool(_DOD.search(text or "")),
    }


def has_behavior_evidence(sig: dict) -> bool:
    """True when the text states or points at expected behavior, so 'no ACs at all' cannot be right."""
    return bool(sig.get("given_when_then_lines") or sig.get("ac_header") or sig.get("spec_headers"))
