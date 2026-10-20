"""Add a generated section (acceptance criteria or implementation plan) to a ticket, safely.

Usage:
    python3 -m scripts.tickets.write --ref PROJ-123 --kind acs  --section-file reports/PROJ-123/acs.md  [--apply]
    python3 -m scripts.tickets.write --ref https://github.com/o/r/issues/42 --kind plan --section-file plan.md --apply [--force]
    python3 -m scripts.tickets.write --ref PROJ-123 --kind plan --section-file why-not.md --apply --comment-only

Nothing is written without --apply (the default is a dry run that says what it would do). Existing content is kept: Jira gets one
panel holding the section appended to the description document, GitHub gets the section between marker comments in the body, and
--force replaces only that panel or block. A second run finds the section and does nothing. The tool refuses tickets an agent must not
rewrite (finished, In Review, blocked, sub-tasks; --allow-ineligible overrides), acceptance criteria while story points are unset or
unreadable (--allow-unpointed overrides), and an implementation plan when one already exists. Every write is read back and compared
(the rest of the ticket is unchanged, the section reads back exactly as rendered). A write the tracker rejected leaves the ticket untouched and
the text is posted as a comment; a write that was accepted but does not read back as expected is reported as written_unverified (no comment:
the text is already on the ticket) for a person to check. Secrets in the text are redacted (the result says when that changed the text).
Story points are never written.
Result: one JSON line {"id","kind","action","verified","tracker","detail"}; action is one of dry_run, appended, replaced, exists,
commented, blocked_points, ineligible, unreadable, failed, written_unverified.
Exit codes: 0 done / dry run / already present, 1 the write failed or could not be verified, 2 invalid arguments, 3 blocked (unreadable or
ineligible ticket, points gate).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.common import adf
from scripts.common.config import load_config
from scripts.common.exit_codes import EXIT_BLOCKED, EXIT_USAGE
from scripts.common.redact import redact
from scripts.tickets import config as readiness_config
from scripts.tickets.fetch import connect
from scripts.tickets.model import TicketError
from scripts.tickets.sections import SECTIONS, contains_markers, has_plan, with_heading

EXIT_FAILED = 1


def _result(ticket_id: str, kind: str, action: str, tracker: str, verified: bool = False, detail: str = "", **extra) -> dict:
    return {"id": ticket_id, "kind": kind, "action": action, "verified": verified, "tracker": tracker, "detail": detail, **extra}


def _points_block(ticket: dict) -> str:
    """Why acceptance criteria must wait ('' when they may be written)."""
    sp = ticket["story_points"]
    if not sp["known"]:
        return f"story points could not be read ({sp['source']}); not writing acceptance criteria on a guess"
    if not sp["value"]:
        return "story points are not set; set them, then write the acceptance criteria"
    return ""


def _ineligible(ticket: dict, cfg: readiness_config.ReadinessConfig) -> str:
    if ticket["is_subtask"]:
        return "sub-tasks take their spec from the parent ticket"
    if ticket["blocked"]:
        return "the ticket is blocked"
    reason = readiness_config.no_enrich_reason(ticket, cfg)
    return f"not rewritten while it is: {reason}" if reason else ""


def _squash(text: str) -> str:
    return " ".join(text.split())


def _verify(client, ref: str, section, markdown: str, outside_before: str) -> bool:
    """Read the ticket back: everything outside the section reads exactly as before, and the section reads as it was rendered."""
    outside, inside = client.read_parts(ref, section)
    return inside is not None and _squash(outside) == _squash(outside_before) and _squash(client.rendered(section, markdown)) in _squash(inside)


def _fallback(client, ref: str, base: tuple[str, str], tracker: str, markdown: str, error: str) -> dict:
    try:
        client.comment(ref, markdown)
        note = " The text was posted as a comment instead."
    except TicketError:
        note = " The text could not be posted as a comment either."
    return _result(*base, "failed", tracker, detail=error + note)


def _already_commented(ticket: dict, markdown: str) -> bool:
    """A comment with the same text exists (compared as plain words, so a re-run does not post it again)."""
    wanted = _squash(adf.to_text({"type": "doc", "content": adf.from_markdown(markdown)})).lower()
    return any(wanted and wanted in _squash(c).lower() for c in ticket["comments"])


def write_section(client, ref: str, ticket: dict, kind: str, markdown: str, cfg: readiness_config.ReadinessConfig, *, apply: bool, force: bool,
                  comment_only: bool, allow_unpointed: bool, allow_ineligible: bool) -> tuple[dict, int]:
    section, tracker = SECTIONS[kind], ticket["tracker"]
    base = (ticket["id"], kind)
    if comment_only:
        if _already_commented(ticket, markdown):
            return _result(*base, "commented", tracker, detail="a comment with this text was already posted; nothing new was added"), 0
        if not apply:
            return _result(*base, "dry_run", tracker, detail="would post a comment", would="comment"), 0
        client.comment(ref, markdown)
        return _result(*base, "commented", tracker, detail="posted; comments are not read back"), 0
    reason = "" if allow_ineligible else _ineligible(ticket, cfg)
    if reason:
        return _result(*base, "ineligible", tracker, detail=reason), EXIT_BLOCKED
    if kind == "acs" and not allow_unpointed:
        reason = _points_block(ticket)
        if reason:
            return _result(*base, "blocked_points", tracker, detail=reason), EXIT_BLOCKED
    exists = client.has_section(ref, section)
    if kind == "plan" and not exists and has_plan(client.read_text(ref)):
        return _result(*base, "exists", tracker, detail="the ticket already has an implementation plan (written by hand or earlier); edit it there"), 0
    if exists and not force:
        return _result(*base, "exists", tracker, detail="the section is already there; use --force to replace it"), 0
    if exists and not client.replaceable(ref, section):
        return _result(*base, "failed", tracker, detail=f"'{section.heading}' is text a person wrote, not a section written by this tool; edit it in the ticket"), EXIT_FAILED
    if not apply:
        return _result(*base, "dry_run", tracker, would="replace" if exists else "append"), 0
    outside_before, _ = client.read_parts(ref, section)
    try:
        client.write_section(ref, section, markdown, replace=exists)
        verified = _verify(client, ref, section, markdown, outside_before)
    except TicketError as e:
        return _fallback(client, ref, base, tracker, markdown, str(e)), EXIT_FAILED
    if not verified:
        return _result(*base, "written_unverified", tracker, detail="the write was accepted but reading the ticket back did not match: the rest of the ticket changed or the section reads differently. Check the ticket; no comment was posted"), EXIT_FAILED
    return _result(*base, "replaced" if exists else "appended", tracker, verified=True), 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--ref", required=True, help="ticket key, #number or URL")
    p.add_argument("--kind", required=True, choices=sorted(SECTIONS))
    p.add_argument("--section-file", required=True)
    p.add_argument("--tracker", default="", choices=["", "github", "jira"])
    p.add_argument("--repo", default="", help="owner/name for GitHub numbers")
    p.add_argument("--config", default="", help="readiness settings file (default: .ygs/readiness.yml when present)")
    p.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    p.add_argument("--force", action="store_true", help="replace the generated section if it exists")
    p.add_argument("--comment-only", action="store_true", help="post the text as a comment, never touch the description")
    p.add_argument("--allow-unpointed", action="store_true", help="write acceptance criteria even when story points are unset")
    p.add_argument("--allow-ineligible", action="store_true", help="write to a finished, In Review, blocked or sub-task ticket (a person's explicit call)")
    a = p.parse_args(argv)
    if a.force and a.comment_only:
        print(json.dumps({"error": "--force and --comment-only exclude each other"}))
        return EXIT_USAGE
    try:
        raw = Path(a.section_file).read_text(encoding="utf-8").strip()
        cfg = readiness_config.load_config(a.config or None)
    except (OSError, ValueError) as e:
        print(json.dumps({"error": str(e)}))
        return EXIT_USAGE
    if not raw:
        print(json.dumps({"error": f"{a.section_file} is empty"}))
        return EXIT_USAGE
    if contains_markers(raw):
        print(json.dumps({"error": "the text contains a section marker comment (<!-- ygs:...-->); remove it, it would corrupt the block"}))
        return EXIT_USAGE
    text = redact(raw)
    try:
        client, ref, tracker = connect(a.ref, a.tracker, a.repo, load_config())
        ticket = client.get(ref)
        if ticket["fetch_error"]:
            print(json.dumps(_result(ticket["id"] or a.ref, a.kind, "unreadable", tracker, detail=ticket["fetch_error"])))
            return EXIT_BLOCKED
        result, code = write_section(client, ref, ticket, a.kind, text if a.comment_only else with_heading(SECTIONS[a.kind], text), cfg,
                                     apply=a.apply, force=a.force, comment_only=a.comment_only, allow_unpointed=a.allow_unpointed,
                                     allow_ineligible=a.allow_ineligible)
    except ValueError as e:  # an unusable reference
        print(json.dumps({"error": str(e)}))
        return EXIT_USAGE
    except TicketError as e:
        print(json.dumps(_result(a.ref, a.kind, "failed", "", detail=str(e))))
        return EXIT_FAILED if e.kind == "failed" else EXIT_BLOCKED
    if text != raw:
        result["redacted"] = True
        result["detail"] = (result["detail"] + " Secrets were redacted from the text; some words may have been replaced.").strip()
    print(json.dumps(result))
    return code


if __name__ == "__main__":
    sys.exit(main())
