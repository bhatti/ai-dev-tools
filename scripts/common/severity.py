"""Severity: the one place that decides what a "major/critical issue" is.

AI review findings carry ``severity`` ``CRITICAL | HIGH | MEDIUM | LOW`` (``findings.json``). Human and bot review
comments on a PR carry free text, so :func:`comment_severity` recognises the markers reviewers and bots really use
(``[critical]``, ``**Major**``, ``Severity: High``, 🔴/🟠, ``P0``/``P1``, a line starting ``Critical:``). A major issue
is CRITICAL or HIGH from any of those sources.
"""

from __future__ import annotations

import re

LEVELS = ("CRITICAL", "HIGH", "MEDIUM", "LOW")

# Vendor wording folded onto our scale (Sonar "blocker/major", review bots "major").
_ALIASES = {"BLOCKER": "CRITICAL", "MAJOR": "HIGH", "MINOR": "LOW", "INFO": "LOW"}

_CRIT = r"(?:critical|blocker)"
_HIGH = r"(?:high|major)"


def _markers(word: str, emoji: str, pnum: str) -> re.Pattern:
    sep = r"(?::|\s[-–—]\s)"   # "Critical: x" / "Critical - x", but not "High-level" or "high-risk"
    return re.compile(
        rf"severity\W{{0,3}}{word}\b"                       # Severity: High
        rf"|\[{word}\]|\*\*{word}\*\*|__{word}__"          # [critical] **Major**
        rf"|(?:^|\n)\s*(?:#+\s*|[-*]\s*)?{emoji}\s*(?:{word}\b|potential issue)"   # a line starting "🔴 Critical"
        rf"|(?:^|\n)\s*[\[(]?{pnum}\b\s*[\]):\-–—]"      # a line starting "P0:" / "[P1]"
        rf"|\b{pnum}\s*[\]):]"                              # "(P1)" / "P0:" inline
        rf"|(?:^|\n)\s*(?:#+\s*|[-*]\s*)?{word}\s*{sep}"   # line starting "Critical:"
        rf"|(?:^|\n)\s*#+\s*{word}\s*(?:\n|$)",           # a heading that is just the level
        re.IGNORECASE,
    )


_CRITICAL_RE = _markers(_CRIT, "🔴", "P0")
_HIGH_RE = _markers(_HIGH, "🟠", "P1")


def normalize(level: str) -> str:
    """Upper-case a severity and fold vendor wording; unknown values come back upper-cased."""
    level = (level or "").strip().upper()
    return _ALIASES.get(level, level)


def count_findings(findings: list[dict] | None) -> dict[str, int]:
    """Counts by level for a ``findings`` list: ``critical, high, medium, low, total`` and ``major`` (critical + high)."""
    counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    findings = findings or []
    for finding in findings:
        level = normalize(str(finding.get("severity", ""))).lower()
        if level in counts:
            counts[level] += 1
    counts["total"] = len(findings)
    counts["major"] = counts["critical"] + counts["high"]
    return counts


def comment_severity(body: str) -> str:
    """``"CRITICAL"``, ``"HIGH"`` or ``""`` for a review comment's text."""
    body = body or ""
    if _CRITICAL_RE.search(body):
        return "CRITICAL"
    if _HIGH_RE.search(body):
        return "HIGH"
    return ""
