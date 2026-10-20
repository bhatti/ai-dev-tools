"""The generated sections this tool adds to tickets, and how to recognise them on either tracker.

Jira stores a description as a document (no HTML comments), so a section is found by its heading; a GitHub body is
markdown, so a section is wrapped in marker comments. "Present" is true for either form on either tracker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

@dataclass(frozen=True)
class Section:
    kind: str
    heading: str
    marker: str

    @property
    def start(self) -> str:
        return f"<!-- {self.marker}:start -->"

    @property
    def end(self) -> str:
        return f"<!-- {self.marker}:end -->"


SECTIONS = {
    "acs": Section("acs", "Acceptance Criteria (generated)", "ygs:acceptance-criteria"),
    "plan": Section("plan", "Implementation Plan (generated)", "ygs:impl-plan"),
}


def with_heading(section: Section, markdown: str) -> str:
    """``markdown`` guaranteed to start with exactly one ``## `` heading, the section's. A first line that is already a heading for the
    same thing (``## Implementation Plan``, with or without '(generated)') is replaced by it, not repeated."""
    body = markdown.strip()
    lines = body.splitlines()
    first = lines[0].strip() if lines else ""
    base = section.heading.split(" (")[0].lower()
    if re.match(r"^#{1,6}\s", first) and first.lstrip("# ").strip().lower().startswith(base):
        return "\n".join([f"## {section.heading}", *lines[1:]])
    return f"## {section.heading}\n{body}"


def wrap(section: Section, markdown: str) -> str:
    return f"{section.start}\n{with_heading(section, markdown)}\n{section.end}"


def _block(section: Section) -> re.Pattern:
    """A start marker, then text without another start marker, then the end marker: a stray start left by a person
    (their end marker deleted) cannot swallow the text between it and our block."""
    start = re.escape(section.start)
    return re.compile(rf"{start}(?:(?!{start}).)*?{re.escape(section.end)}", re.S)


def has_marked(text: str, section: Section) -> bool:
    return bool(_block(section).search(text or ""))


def append_marked(text: str, section: Section, markdown: str) -> str:
    return f"{(text or '').rstrip()}\n\n{wrap(section, markdown)}\n"


def replace_marked(text: str, section: Section, markdown: str) -> str:
    """``text`` with the marked block replaced; KeyError when there is none."""
    if not has_marked(text, section):
        raise KeyError(section.marker)
    return _block(section).sub(lambda _: wrap(section, markdown), text, count=1)


def split_marked(text: str, section: Section) -> tuple[str, str | None]:
    """(text outside the marked block, the block or None): used to verify a write left everything else alone."""
    m = _block(section).search(text or "")
    return (text, None) if not m else ((text[:m.start()] + text[m.end():]), m.group(0))


def contains_markers(text: str) -> bool:
    """True when ``text`` itself holds a section marker; such text could end a block early, so it is refused."""
    return any(marker in (text or "") for s in SECTIONS.values() for marker in (s.start, s.end))


def has_plan(text: str) -> bool:
    """True when the description already carries an implementation plan: this tool's markers, or a heading called
    'Implementation plan' (written by hand or by an earlier version of the skill, which put that heading inside its markers)."""
    low = (text or "").lower()
    return SECTIONS["plan"].start.lower() in low or bool(re.search(r"^#{1,6}\s*implementation plan\b", low, re.M))
