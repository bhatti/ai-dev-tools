"""Fetch tickets (GitHub issues or Jira issues) as one normalized JSON shape for the readiness tools.

Usage:
    python3 -m scripts.tickets.fetch --sprint "Sprint 12" [--project PROJ] --out ready-out/tickets.json      (Jira)
    python3 -m scripts.tickets.fetch --milestone "Sprint 12" [--repo owner/name] --out ready-out/tickets.json (GitHub)
    python3 -m scripts.tickets.fetch PROJ-123 https://github.com/o/r/issues/42 --out tickets.json

Scope: refs, or one of --sprint / --label / --milestone / --jql. Done tickets are skipped unless --include-done (an explicit --jql is used as written).
Tracker: --tracker, else the refs' URLs, else DEFAULT_TRACKER. Credentials: JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN, or
`gh auth`; GitHub repository: --repo or GH_ORG and GH_REPO; optional GH_PROJECT_NUMBER reads points from a Projects number field.
With --out, every ticket is also written as <out dir>/tickets/<id>.txt (real line breaks, long lines wrapped): the file to read when
a description is too long for one line of the JSON. A ticket that cannot be read is kept as an UNREADABLE entry; points that cannot be
read are `known: false`, never "unset".
Exit codes: 0 ok, 2 invalid arguments, 3 nothing trustworthy could be fetched (credentials, search failure, no matching ticket).
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

from scripts.common.config import load_config
from scripts.common.exit_codes import EXIT_BLOCKED, EXIT_USAGE
from scripts.skill.flags import SkillFlags, resolve_tracker
from scripts.tickets import github, jira
from scripts.tickets.model import TicketError, unreadable

JIRA_REQUIRED = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")


def detect_tracker(refs: list[str], explicit: str, config: dict) -> str:
    """--tracker, else what the refs look like (GitHub URL, Jira key or /browse/ URL), else DEFAULT_TRACKER."""
    hint = ""
    for ref in refs:
        if "github.com" in ref:
            hint = "github"
        elif "/browse/" in ref or jira.key_of(ref):
            hint = "jira"
    tracker = resolve_tracker(SkillFlags(tracker=explicit or hint), config)
    if tracker not in ("github", "jira"):
        raise ValueError(f"tracker must be github or jira, got {tracker!r}")
    return tracker


def repo_of(repo: str, config: dict) -> str:
    """owner/name from ``--repo``, else GH_ORG and GH_REPO, else ''."""
    if repo:
        return repo
    org, name = config.get("GH_ORG", ""), config.get("GH_REPO", "")
    return f"{org}/{name}" if org and name else ""


def connect(ref: str, tracker_flag: str, repo_flag: str, config: dict) -> tuple[object, str, str]:
    """(client, ref as the client wants it, tracker) for one ticket reference; TicketError when it cannot be reached."""
    tracker = detect_tracker([ref], tracker_flag, config)
    if tracker == "jira":
        missing = [k for k in JIRA_REQUIRED if not config.get(k)]
        if missing:
            raise TicketError(f"missing Jira credentials: {', '.join(missing)}", "access")
        return jira.JiraTickets(config), jira.key_of(ref) or ref, tracker
    repo, number = github.parse_ref(ref, repo_of(repo_flag, config))
    return github.GitHubTickets(repo, config.get("GH_PROJECT_NUMBER", "")), number, tracker


def _jira(args: argparse.Namespace, config: dict) -> tuple[list[dict], str]:
    missing = [k for k in JIRA_REQUIRED if not config.get(k)]
    if missing:
        raise TicketError(f"missing Jira credentials: {', '.join(missing)}", "access")
    source = jira.JiraTickets(config)
    if args.refs:
        return [source.get(r) for r in args.refs], " ".join(args.refs)
    query = jira.build_jql(sprint=args.sprint, label=args.label, project=args.project or config.get("JIRA_PROJECT", ""),
                           jql=args.jql, include_done=args.include_done)
    return source.search(query, args.limit), query


def _github(args: argparse.Namespace, config: dict) -> tuple[list[dict], str]:
    project = config.get("GH_PROJECT_NUMBER", "")
    default_repo = repo_of(args.repo, config)
    if args.refs:
        tickets = []
        for ref in args.refs:
            try:
                client, number, _ = connect(ref, "github", args.repo, config)
            except TicketError as e:  # one bad reference must not fail the batch
                tickets.append(unreadable("github", ref, str(e)))
                continue
            tickets.append(client.get(number))
        return tickets, " ".join(args.refs)
    if args.jql:
        raise ValueError("--jql is Jira only")
    milestone = args.milestone or args.sprint  # GitHub has no sprints: the milestone is the sprint
    tickets = github.GitHubTickets(default_repo, project).search(label=args.label, milestone=milestone, include_done=args.include_done, limit=args.limit)
    return tickets, f"{default_repo} label={args.label} milestone={milestone}".strip()


def collect(args: argparse.Namespace, config: dict) -> dict:
    tracker = detect_tracker(args.refs, args.tracker, config)
    tickets, scope = (_jira if tracker == "jira" else _github)(args, config)
    truncated = not args.refs and len(tickets) >= args.limit
    if truncated:
        print(f"[tickets] warning: {len(tickets)} tickets is the --limit; the scope may hold more. Re-run with a higher --limit.", file=sys.stderr)
    return {"tracker": tracker, "scope": scope, "count": len(tickets), "truncated": truncated, "tickets": tickets,
            "errors": [{"id": t["id"], "error": t["fetch_error"]} for t in tickets if t["fetch_error"]]}


READABLE_LINE = 1200  # lines longer than this are wrapped in the text files (the model's file reader cuts very long lines)


def file_id(ticket_id: str) -> str:
    """A ticket id as a file name: PROJ-123 stays, #42 becomes gh-42."""
    return "gh-" + ticket_id[1:] if ticket_id.startswith("#") else ticket_id.replace("/", "_")


def _wrap(text: str) -> str:
    lines = []
    for line in text.splitlines():
        lines += textwrap.wrap(line, READABLE_LINE, break_long_words=False, break_on_hyphens=False, replace_whitespace=False, drop_whitespace=False) if len(line) > READABLE_LINE else [line]
    return "\n".join(lines)


def ticket_text(t: dict) -> str:
    """One ticket as plain text: facts first, then the complete description, environment and every comment."""
    if t["fetch_error"]:
        return f"# {t['id']} — could not be read\n{t['fetch_error']}\n"
    sp = t["story_points"]
    points = f"{sp['value']:g}" if sp["value"] else ("unknown (could not be read)" if not sp["known"] else "not set")
    facts = [f"Tracker: {t['tracker']}", f"Type: {t['type']}", f"Status: {t['status']} ({t['status_category']})", f"Assignee: {t['assignee'] or 'unassigned'}",
             f"Labels: {', '.join(t['labels']) or 'none'}", f"Story points: {points}"]
    if t["blocked_by"]:
        facts.append(f"Blocked by: {', '.join(t['blocked_by'])}")
    out = [f"# {t['id']} — {t['title']}", " · ".join(facts), f"URL: {t['url']}" if t["url"] else ""]
    if t["parent"]:
        out += ["", f"Parent ticket {t['parent']['id']} — {t['parent'].get('title', '')}:", t["parent"].get("description") or "(no description)"]
        if t["parent"].get("error"):
            out.append(f"(the parent could not be read: {t['parent']['error']})")
    out += ["", "## Description", t["description"] or "(empty)"]
    if t.get("environment"):
        out += ["", "## Environment", t["environment"]]
    for i, comment in enumerate(t["comments"], 1):
        out += ["", f"## Comment {i} of {len(t['comments'])}", comment]
    return _wrap("\n".join(out)) + "\n"


def write_text_files(result: dict, out_file: Path) -> None:
    """tickets/<id>.txt next to the JSON; each ticket records its file."""
    folder = out_file.parent / "tickets"
    folder.mkdir(parents=True, exist_ok=True)
    for t in result["tickets"]:
        path = folder / f"{file_id(t['id'])}.txt"
        path.write_text(ticket_text(t), encoding="utf-8")
        t["text_file"] = str(path)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("refs", nargs="*", help="ticket keys, #numbers or URLs")
    p.add_argument("--sprint", default="")
    p.add_argument("--label", default="")
    p.add_argument("--milestone", default="")
    p.add_argument("--jql", default="")
    p.add_argument("--project", default="")
    p.add_argument("--repo", default="", help="owner/name for GitHub")
    p.add_argument("--tracker", default="", choices=["", "github", "jira"])
    p.add_argument("--include-done", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--out", default="")
    a = p.parse_args(argv)
    scopes = [bool(a.sprint), bool(a.label), bool(a.milestone), bool(a.jql)]
    if (not a.refs and not any(scopes)) or (a.refs and any(scopes)) or (a.jql and sum(scopes) > 1) or (a.milestone and a.sprint):
        print(json.dumps({"error": "give ticket refs, or --jql alone, or --sprint / --label (Jira) or --milestone / --label (GitHub)"}))
        return EXIT_USAGE
    if a.limit < 1:
        print(json.dumps({"error": "--limit must be positive"}))
        return EXIT_USAGE
    try:
        result = collect(a, load_config())
    except ValueError as e:
        print(json.dumps({"error": str(e)}))
        return EXIT_USAGE
    except TicketError as e:
        print(json.dumps({"error": str(e), "kind": e.kind}))
        return EXIT_BLOCKED
    if not result["tickets"]:
        print(json.dumps({"error": f"no tickets matched ({result['scope']}); check the sprint, label or milestone name"}))
        return EXIT_BLOCKED
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        write_text_files(result, Path(a.out))
    text = json.dumps(result, indent=2)
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"[tickets] wrote {a.out} (tracker={result['tracker']} tickets={result['count']} unreadable={len(result['errors'])})")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
