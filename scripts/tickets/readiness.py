"""Compute ticket readiness verdicts, the report and the final status line from fetched tickets and the model's grades.

The model reads each ticket and writes ``grades.json`` (Pass / Partial / Missing with quoted evidence); everything that can be
decided from data (story points, blocked, sub-task, plan present, fetch errors) comes from the fetch result, and the status,
counts, report and JSON are computed here, so the same input always gives the same verdict.

Writes reports/report.md, report.html, result.json and slack.md (the short summary the Slack post uses).

Usage:
    python3 -m scripts.tickets.readiness --tickets ready-out/tickets.json --grades ready-out/grades.json \\
        [--actions ready-out/actions.json] [--config .ygs/readiness.yml] [--capacity 40] [--dry-run] --out-dir reports

grades.json:  {"tickets": [{"id", "checks": {"why","scope","acs","dod","variants"}, "says", "evidence", "questions" (strings or {question, recommend, because}),
               "decisions_made", "still_needed" {landed, duplicate_of, spec_drift}, "ac_starter", "variant_holes", "suggested_points"}]}      (grade values: pass | partial | missing | na)
actions.json: the JSON result lines printed by scripts.tickets.write, as a list ({"id","kind","action",...}), or entries
              {"id", "action": "acs_written|acs_blocked_points|plan_written|plan_low_confidence|skipped|preview"}
Exit codes: 0 report written, 2 invalid input, 3 BLOCKED (no ticket could be read). The last output line is the status line.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from scripts.common.exit_codes import EXIT_BLOCKED, EXIT_USAGE
from scripts.common.redact import redact
from scripts.common.report_bundle import md_cell, write_bundle
from scripts.tickets import load, load_report, slack_text
from scripts.tickets.config import ReadinessConfig, load_config, no_enrich_reason
from scripts.tickets.signals import has_behavior_evidence

GRADES = ("pass", "partial", "missing", "na")
CHECKS = ("why", "scope", "acs", "dod", "variants")
ENRICHABLE = ("NEEDS_AC_WORK", "NEEDS_AC_CLARIFICATION", "NEEDS_IMPL_PLAN")
NEEDS_HUMAN = ("UNREADABLE", "UNGRADED", "BLOCKED", "NEEDS_STORY_POINTS", "HAS_VARIANT_HOLES", "SUBTASK_NEEDS_CONTEXT",
               "IMPL_PLAN_LOW_CONFIDENCE", "ENRICHED_PENDING_POINTS")
CLEAN = ("READY", "ENRICHED", "SUBTASK_OK")
ORDER = ("UNREADABLE", "UNGRADED", "BLOCKED", "NEEDS_AC_WORK", "NEEDS_AC_CLARIFICATION", "NEEDS_STORY_POINTS", "NEEDS_IMPL_PLAN",
         "HAS_VARIANT_HOLES", "IMPL_PLAN_LOW_CONFIDENCE", "ENRICHED_PENDING_POINTS", "SUBTASK_NEEDS_CONTEXT", "READY", "ENRICHED", "SUBTASK_OK")
SPLIT_AT = 21
MIN_CONTEXT_CHARS = 20


def index_grades(raw: dict) -> dict[str, dict]:
    """Grades by ticket id; unknown grade values and duplicate ids raise ValueError (a typo must not become a verdict)."""
    out: dict[str, dict] = {}
    for g in raw.get("tickets", []):
        if not g.get("id"):
            raise ValueError("a grade has no id")
        if g["id"] in out:
            raise ValueError(f"duplicate grade for {g['id']}")
        for name, value in (g.get("checks") or {}).items():
            if name not in CHECKS or value not in GRADES:
                raise ValueError(f"{g['id']}: {name}={value!r} is not valid (checks: {', '.join(CHECKS)}; values: {', '.join(GRADES)})")
        _check_grade_text(g)
        out[g["id"]] = g
    return out


_WRITTEN = {"appended", "replaced", "exists"}


def _check_grade_text(g: dict) -> None:
    """The free-text parts of a grade have a fixed shape (the report renders them): questions are strings or {question, recommend, because}."""
    for q in g.get("questions") or []:
        if not (isinstance(q, str) and q.strip()) and not (isinstance(q, dict) and isinstance(q.get("question"), str) and q["question"].strip()):
            raise ValueError(f"{g['id']}: each question is a string or {{question, recommend, because}}")
    if "questions" in g and not isinstance(g["questions"], list):
        raise ValueError(f"{g['id']}: questions must be a list")
    if "decisions_made" in g and not (isinstance(g["decisions_made"], list) and all(isinstance(d, str) for d in g["decisions_made"])):
        raise ValueError(f"{g['id']}: decisions_made must be a list of strings")
    if "still_needed" in g and not isinstance(g["still_needed"], dict):
        raise ValueError(f"{g['id']}: still_needed must be {{landed, duplicate_of, spec_drift}}")


def canonical_action(item: dict) -> str:
    """The action name for an entry of actions.json: either already canonical, or a result line printed by scripts.tickets.write."""
    action, kind = item.get("action", ""), item.get("kind", "")
    if kind == "acs":
        return "acs_written" if action in _WRITTEN else "acs_blocked_points" if action == "blocked_points" else "preview"
    if kind == "plan":
        return "plan_written" if action in _WRITTEN else "plan_low_confidence" if action == "commented" else "preview"
    return action


def unknown_grade_ids(tickets: list[dict], grades: dict[str, dict]) -> list[str]:
    return sorted(set(grades) - {t["id"] for t in tickets})


def _points_state(t: dict, cfg: ReadinessConfig) -> tuple[str, str]:
    """(state, gap): ok | unset | off_scale | split | unknown."""
    sp = t["story_points"]
    if not sp["known"]:
        return "unknown", f"story points could not be read ({sp['source']})"
    value = sp["value"]
    if not value:
        return "unset", "story points are not set"
    if value >= SPLIT_AT:
        return "split", f"{value:g} points is too large: split the ticket"
    if value not in cfg.points_scale:
        return "off_scale", f"{value:g} points is not on the scale ({', '.join(f'{x:g}' for x in cfg.points_scale)})"
    return "ok", ""


def _effective_acs(t: dict, checks: dict, notes: list[str]) -> str:
    grade = checks.get("acs", "missing")
    if grade == "missing" and has_behavior_evidence(t["signals"]):
        notes.append("ACs raised from missing to partial: the ticket text states behavior (evidence rule)")
        return "partial"
    return grade


def _status(t: dict, g: dict | None, cfg: ReadinessConfig, actions: list[str], detail: dict) -> str:
    if t["fetch_error"]:
        return "UNREADABLE"
    if g is None:
        return "UNGRADED"
    checks, notes = g.get("checks") or {}, detail["notes"]
    if t["is_subtask"]:
        if (t["parent"] or {}).get("error"):
            detail["concerns"].append(t["parent"]["error"])
        return "SUBTASK_NEEDS_CONTEXT" if checks.get("why", "missing") == "missing" else "SUBTASK_OK"
    if t["blocked"] or (checks.get("why") == "missing" and len(t["description"]) < MIN_CONTEXT_CHARS):
        return "BLOCKED"
    acs = "pass" if {"acs_written", "acs_blocked_points"} & set(actions) else _effective_acs(t, checks, notes)
    has_plan = t["has_impl_plan"] or "plan_written" in actions
    points, points_gap = _points_state(t, cfg)
    if points_gap:
        (detail["concerns"] if points == "unknown" else detail["gaps"]).append(points_gap)
    if acs == "missing":
        return "NEEDS_AC_WORK"
    if acs == "partial":
        return "NEEDS_AC_CLARIFICATION"
    if "acs_blocked_points" in actions:
        return "ENRICHED_PENDING_POINTS"
    if points in ("unset", "off_scale", "split"):
        return "NEEDS_STORY_POINTS"
    if not has_plan:
        return "IMPL_PLAN_LOW_CONFIDENCE" if "plan_low_confidence" in actions else "NEEDS_IMPL_PLAN"
    if cfg.variants and (g.get("variant_holes") or checks.get("variants") in ("partial", "missing")):
        return "HAS_VARIANT_HOLES"
    return "ENRICHED" if {"acs_written", "plan_written"} & set(actions) else "READY"


def evaluate(tickets: list[dict], grades: dict[str, dict], cfg: ReadinessConfig, actions: dict[str, list[str] | str]) -> list[dict]:
    """One row per ticket: status, gaps, concerns, notes, enrichability and the grader's text. Order follows ``tickets``."""
    rows = []
    for t in tickets:
        g = grades.get(t["id"])
        acts = actions.get(t["id"], [])
        acts = [acts] if isinstance(acts, str) else list(acts)
        detail: dict = {"gaps": [], "concerns": [], "notes": []}
        status = _status(t, g, cfg, acts, detail)
        checks = (g or {}).get("checks") or {}
        evidence = (g or {}).get("evidence") or {}
        for name in CHECKS:
            if (name == "variants" and not cfg.variants) or (name == "acs" and {"acs_written", "acs_blocked_points"} & set(acts)):
                continue
            if checks.get(name) in ("partial", "missing"):
                detail["gaps"].append(f"{name}: {checks[name]}" + (f" ({evidence[name]})" if evidence.get(name) else ""))
        enrich_blocked = no_enrich_reason(t, cfg)
        rows.append({
            "id": t["id"], "title": t["title"], "url": t["url"], "type": t["type"], "tracker_status": t["status"], "in_flight": t["in_flight"],
            "assignee": t["assignee"], "story_points": t["story_points"]["value"], "status": status, "checks": checks,
            "gaps": detail["gaps"], "concerns": detail["concerns"], "notes": detail["notes"], "fetch_error": t["fetch_error"],
            "says": (g or {}).get("says", ""), "evidence": evidence, "questions": (g or {}).get("questions") or [],
            "ac_starter": (g or {}).get("ac_starter", ""), "suggested_points": (g or {}).get("suggested_points"),
            "lane": t["lane"], "carry_over": t["carry_over"], "decisions_made": (g or {}).get("decisions_made") or [],
            "still_needed": {k: v for k, v in ((g or {}).get("still_needed") or {}).items() if v},
            "test_lines": t["signals"]["test_lines"], "gwt_lines": t["signals"]["given_when_then_lines"],
            "auto_enrichable": status in ENRICHABLE and not enrich_blocked, "enrich_blocked": enrich_blocked,
        })
    return rows


def summarize(rows: list[dict]) -> dict:
    counts = Counter(r["status"] for r in rows)
    return {"total": len(rows), "ready": counts["READY"], "ready_with_gaps": sum(1 for r in rows if r["status"] == "READY" and r["gaps"]),
            "enriched": counts["ENRICHED"], "enriched_pending_points": counts["ENRICHED_PENDING_POINTS"], "unreadable": counts["UNREADABLE"],
            "ungraded": counts["UNGRADED"], "needs_human": sum(counts[s] for s in NEEDS_HUMAN),
            "auto_enrichable": sum(1 for r in rows if r["auto_enrichable"]), "by_status": {s: counts[s] for s in ORDER if counts[s]}}


def status_line(rows: list[dict], truncated: bool = False, load_flags: int = 0) -> dict:
    """The line automation reads: DONE, DONE_WITH_CONCERNS or BLOCKED (nothing readable). A scope cut at the fetch limit is a concern."""
    s = summarize(rows)
    if not rows or s["unreadable"] == len(rows):
        return {"status": "ERROR", "signal": "BLOCKED", "total": s["total"], "ready": 0, "needs_human": s["needs_human"], "auto_enrichable": 0,
                "summary": "no ticket could be read" if rows else "no tickets to evaluate"}
    flagged = sum(1 for r in rows if r["status"] in CLEAN and r["concerns"])
    clean = all(r["status"] in CLEAN for r in rows) and not truncated and not flagged and not load_flags
    parts = ([f"{n} {name}" for name, n in s["by_status"].items() if name not in CLEAN] + ([f"{flagged} with something that could not be read"] if flagged else [])
             + (["scope truncated at the fetch limit"] if truncated else []) + ([f"{load_flags} sprint-load decision(s) open"] if load_flags else []))
    return {"status": "DONE", "signal": "DONE" if clean else "DONE_WITH_CONCERNS", "total": s["total"], "ready": s["ready"],
            "needs_human": s["needs_human"], "auto_enrichable": s["auto_enrichable"],
            "summary": "every ticket is ready" if clean else "; ".join(parts)}


def _checklist(r: dict) -> str:
    icon = {"pass": "✅", "partial": "⚠️", "missing": "❌", "na": "—"}
    pts = "❌" if r["story_points"] in (None, 0) else f"✅ {r['story_points']:g}"
    c = r["checks"]
    return ("| Why | Points | Scope | ACs | DoD | Variants |\n| :-: | :-: | :-: | :-: | :-: | :-: |\n"
            f"| {icon.get(c.get('why'), '?')} | {pts} | {icon.get(c.get('scope'), '?')} | {icon.get(c.get('acs'), '?')} | "
            f"{icon.get(c.get('dod'), '?')} | {icon.get(c.get('variants', 'na'), '—')} |")


def _question(q: str | dict) -> str:
    """A question as a line: with the grader's recommended answer and the reason when it gave them."""
    if isinstance(q, str):
        return md_cell(q)
    rec = f" _Recommend: {md_cell(q['recommend'])}" + (f", because {md_cell(q['because'])}" if q.get("because") else "") + "._" if q.get("recommend") else ""
    return md_cell(q["question"]) + rec


def _card(r: dict) -> list[str]:
    ref = f"[{r['id']}]({r['url']})" if slack_text.SAFE_URL.match(r["url"]) and ")" not in r["url"] else md_cell(r["id"])
    if r["fetch_error"]:
        return [f"### {ref} — (unreadable)", f"{r['status']}", f"**Could not be read:** {md_cell(r['fetch_error'])}", ""]
    carried = f"carried over {r['carry_over']['times']}× ({md_cell(', '.join(r['carry_over']['from_sprints']))})" if r["carry_over"]["times"] else ""
    meta_line = " · ".join(x for x in (md_cell(r["type"]), r["status"], "in-flight" if r["in_flight"] else "", md_cell(r["tracker_status"]), md_cell(r["assignee"] or "unassigned"), carried) if x)
    head = [f"### {ref} — {md_cell(r['title'])}", meta_line]
    lines = [*head, "", _checklist(r), ""]
    if r["says"]:
        lines.append(f"**What the ticket says:** \"{md_cell(r['says'])}\"")
    for gap in r["gaps"]:
        lines.append(f"- **Missing:** {md_cell(gap)}")
    lines += [f"- {md_cell(c)}" for c in r["concerns"]] + [f"- _{md_cell(n)}_" for n in r["notes"]]
    if r["enrich_blocked"]:
        lines.append(f"- Not enriched automatically: status is {md_cell(r['enrich_blocked'])}")
    if r["still_needed"]:
        sn = r["still_needed"]
        found = [f"already merged: {md_cell(sn['landed'])}"] if sn.get("landed") else []
        found += [f"duplicate of {md_cell(sn['duplicate_of'])}"] if sn.get("duplicate_of") else []
        found += [f"spec and code disagree: {md_cell(sn['spec_drift'])}"] if sn.get("spec_drift") else []
        lines += ["", f"**Still needed?** {'; '.join(found)}"]
    if r["questions"]:
        lines += ["", "**Clarify (answer in the ticket):**"] + [f"{i}. {_question(q)}" for i, q in enumerate(r["questions"], 1)]
    if r["decisions_made"]:
        lines += ["", "**Decisions made for you — confirm:**"] + [f"- {md_cell(d)}" for d in r["decisions_made"]]
    if r["ac_starter"]:
        lines += ["", f"**Paste-ready AC starter:** {md_cell(r['ac_starter'])}"]
    if r["suggested_points"]:
        lines.append(f"**Suggested points (for a person to confirm):** {r['suggested_points']}")
    return [*lines, ""]


def pattern_buckets(rows: list[dict]) -> list[tuple[str, list[str]]]:
    """(gap, ticket ids) for gaps seen across tickets, most common first."""
    graded = [r for r in rows if r["checks"] and r["status"] not in ("SUBTASK_OK", "SUBTASK_NEEDS_CONTEXT")]
    buckets: dict[str, list[str]] = defaultdict(list)
    for r in graded:
        if r["checks"].get("acs") == "partial" or r["status"] == "NEEDS_AC_CLARIFICATION":
            buckets["ACs partial (not Given/When/Then)"].append(r["id"])
        if r["checks"].get("acs") == "missing" and r["status"] == "NEEDS_AC_WORK":
            buckets["ACs missing"].append(r["id"])
        if r["gwt_lines"] and not r["test_lines"]:
            buckets["ACs without a Test: line"].append(r["id"])
        if r["story_points"] in (None, 0):
            buckets["Story points unset"].append(r["id"])
        for name, label in (("why", "Why missing or partial"), ("scope", "Scope missing or partial"), ("dod", "DoD missing or partial")):
            if r["checks"].get(name) in ("partial", "missing"):
                buckets[label].append(r["id"])
    return sorted(buckets.items(), key=lambda kv: -len(kv[1]))


def action_items(rows: list[dict], sprint: dict | None = None) -> list[tuple[str, list[str]]]:
    """(who, ticket ids) for what has to happen next, the sprint-load decisions first; empty groups are left out."""
    def ids(*statuses: str) -> list[str]:
        return [r["id"] for r in rows if r["status"] in statuses]
    items = [("Engineers (answer the clarifying questions)", ids("NEEDS_AC_CLARIFICATION")),
             ("Engineers or PM (set story points)", ids("NEEDS_STORY_POINTS", "ENRICHED_PENDING_POINTS")),
             ("PM or tech lead (decide variant scope)", ids("HAS_VARIANT_HOLES")),
             ("Tech lead (missing context or blockers)", ids("BLOCKED", "SUBTASK_NEEDS_CONTEXT", "IMPL_PLAN_LOW_CONFIDENCE")),
             ("Anyone with access (ticket could not be read)", ids("UNREADABLE")),
             ("Grading was not recorded (re-run grading)", ids("UNGRADED")),
             ("Engineers (work already landed or duplicated; close or narrow the ticket)", [r["id"] for r in rows if r["still_needed"].get("landed") or r["still_needed"].get("duplicate_of")]),
             ("An agent can enrich these (run ygs-enrich-ticket)", [r["id"] for r in rows if r["auto_enrichable"]])]
    return [*(load_report.action_items(sprint["summary"], sprint["flags"]) if sprint else []), *((who, tickets) for who, tickets in items if tickets)]


def _patterns(rows: list[dict]) -> list[str]:
    buckets = pattern_buckets(rows)
    if not buckets:
        return ["No recurring gaps."]
    return ["| Gap | Count | Tickets |", "| --- | ---: | --- |"] + [f"| {label} | {len(ids)} | {', '.join(ids)} |" for label, ids in buckets]


def _actions(rows: list[dict], sprint: dict | None = None) -> list[str]:
    return [f"- **{who}:** {', '.join(ids)}" for who, ids in action_items(rows, sprint)] or ["Nothing to do: every ticket is ready."]


# ── Slack ─────────────────────────────────────────────────────────────────────

SLACK_TICKET_LINES = 15
SLACK_LINE_CHARS = 160
SLACK_MAX_CHARS = 9000


def _slack_ref(r: dict) -> str:
    ident = slack_text.escape(r["id"]).replace("`", "'")
    return f"<{r['url']}|{ident}>" if slack_text.SAFE_URL.match(r["url"]) else f"`{ident}`"


def _short_gaps(r: dict) -> str:
    if r["fetch_error"]:
        return slack_text.clip(r["fetch_error"], SLACK_LINE_CHARS)
    labels = {"why": "Why", "scope": "scope", "acs": "ACs", "dod": "DoD"}
    parts = [f"{labels[name]} {grade}" for name, grade in r["checks"].items() if grade in ("partial", "missing") and name in labels]
    parts += [slack_text.clip(g, 60) for g in r["gaps"] if g.startswith("story points")]
    if r["status"] == "BLOCKED" and r["checks"].get("why") != "missing":
        parts.insert(0, "blocked")
    return ", ".join(dict.fromkeys(parts[:4])) or "see the full report"


def render_slack(rows: list[dict], meta: dict, previews: dict | None = None, sprint: dict | None = None) -> str:
    """A short mrkdwn summary for Slack: the verdict, who must do what, the biggest gaps and one line per ticket that needs attention
    (tables do not render in Slack; the full cards are in report.md / report.html). Capped for sprints of any size."""
    s = summarize(rows)
    head = (f"*Ticket readiness* · {slack_text.escape(meta.get('tracker', ''))} · {slack_text.escape(meta.get('scope', ''))} · {s['total']} ticket(s)"
            + (" (dry-run)" if meta.get("dry_run") else "") + (" · :warning: scope truncated at the fetch limit" if meta.get("truncated") else ""))
    lines = [head, f":white_check_mark: Ready {s['ready']} · :raising_hand: Needs a person {s['needs_human']} · :wrench: An agent can enrich {s['auto_enrichable']}", ""]
    if sprint:
        lines += load_report.slack(sprint["summary"], sprint["flags"], sprint["capacity"]) + [""]
    lines += ["*By status*"] + [f"• {name} ({n}): " + slack_text.ids([r["id"] for r in rows if r["status"] == name]) for name, n in s["by_status"].items()]
    todo = action_items(rows, sprint)
    if todo:
        lines += ["", "*Next*"] + [f"• {who}: {slack_text.ids(ids)}" for who, ids in todo]
    top = pattern_buckets(rows)[:3]
    if top:
        lines += ["", "*Most common gaps*: " + "; ".join(f"{label} ({len(ids)})" for label, ids in top)]
    attention = [r for r in rows if r["status"] not in ("READY", "ENRICHED", "SUBTASK_OK")]
    if attention:
        lines += ["", "*Tickets that need attention*"]
        for r in attention[:SLACK_TICKET_LINES]:
            title = slack_text.clip(r["title"], 70) if r["title"] else "(unreadable)"
            lines.append(f"• {_slack_ref(r)} {title} · {r['status']} · {_short_gaps(r)}")
        if len(attention) > SLACK_TICKET_LINES:
            lines.append(f"…and {len(attention) - SLACK_TICKET_LINES} more not listed here")
    lines += ["", "Questions and paste-ready acceptance criteria for each ticket are in the full report."
              + (f" Generated acceptance criteria / plan previews for {len(previews)} ticket(s) are in its Previews section." if previews else "")]
    text = "\n".join(lines) + "\n"
    if len(text) > SLACK_MAX_CHARS:  # a safety net: the per-line caps keep real messages far below this
        text = text[:SLACK_MAX_CHARS].rsplit("\n", 1)[0] + "\n…shortened; see the full report\n"
    return text


def load_previews(out_dir: Path, rows: list[dict]) -> dict[str, dict[str, str]]:
    """The previews the enrich skill saved under <out dir>/<id>/acs.md and plan.md, for the tickets in this report only."""
    found: dict[str, dict[str, str]] = {}
    for r in rows:
        folder = out_dir / r["id"].replace("#", "gh-").replace("/", "_")
        texts = {kind: (folder / f"{kind}.md").read_text(encoding="utf-8").strip() for kind in ("acs", "plan") if (folder / f"{kind}.md").is_file()}
        if texts:
            found[r["id"]] = texts
    return found


def render_markdown(rows: list[dict], meta: dict, previews: dict[str, dict[str, str]] | None = None, sprint: dict | None = None) -> str:
    s = summarize(rows)
    lines = ["# Ticket readiness", f"{md_cell(meta.get('tracker', ''))} · {md_cell(meta.get('scope', ''))} · {s['total']} ticket(s)"
             + (" · dry-run" if meta.get("dry_run") else "") + (" · TRUNCATED at the fetch limit: the scope holds more tickets" if meta.get("truncated") else ""),
             "", "## Scoreboard", "", "| Status | Count | Tickets |", "| --- | ---: | --- |"]
    for status, n in s["by_status"].items():
        lines.append(f"| {status} | {n} | {', '.join(r['id'] for r in rows if r['status'] == status)} |")
    lines += ["", f"Ready: {s['ready']} · Needs a person: {s['needs_human']} · An agent can enrich: {s['auto_enrichable']}", ""]
    if sprint:
        lines += load_report.markdown(sprint["summary"], sprint["flags"], sprint["capacity"]) + [""]
    attention = [r for r in rows if r["status"] not in ("READY", "ENRICHED", "SUBTASK_OK")]
    lines += ["## Needs attention", ""]
    for status in ORDER:
        group = [r for r in attention if r["status"] == status]
        if group:
            lines += [f"#### {status} ({len(group)})", ""]
            for r in group:
                lines += _card(r)
    if not attention:
        lines += ["Nothing needs attention.", ""]
    ready = [r for r in rows if r["status"] in ("READY", "ENRICHED")]
    lines += ["## Ready", ""] + ([f"- {r['id']} — {md_cell(r['title'])}" + (f" ({r['story_points']:g} points)" if r["story_points"] else "")
                                  + (" · gaps: " + md_cell("; ".join(r["gaps"])) if r["gaps"] else "") for r in ready] or ["None."])
    subs = [r for r in rows if r["status"] == "SUBTASK_OK"]
    lines += ["", "## Sub-tasks", ""] + ([f"- {r['id']} — {md_cell(r['title'])}" for r in subs] or ["None."])
    lines += ["", "## Patterns", ""] + _patterns(rows) + ["", "## Action items", ""] + _actions(rows, sprint)
    if previews:
        lines += ["", "## Previews", "", "Generated text for tickets that were not written (or were): review it, then run again with `--apply`.", ""]
        for ticket_id, texts in previews.items():
            lines += [f"### {md_cell(ticket_id)}", ""] + [f"{texts[kind]}\n" for kind in ("acs", "plan") if kind in texts]
    return "\n".join(lines) + "\n"


def _load(path: str, what: str) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"cannot read {what} {path!r}: {e}") from e


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--tickets", required=True)
    p.add_argument("--grades", required=True)
    p.add_argument("--actions", default="")
    p.add_argument("--config", default="")
    p.add_argument("--capacity", default=None, help="team capacity in story points, compared with the open (not done) load")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--out-dir", default="reports")
    a = p.parse_args(argv)
    try:
        capacity = float(a.capacity) if a.capacity is not None else None
        if capacity is not None and not (capacity > 0 and math.isfinite(capacity)):
            raise ValueError
    except ValueError:
        print(json.dumps({"error": f"--capacity must be a number above 0, got {a.capacity!r}"}))
        return EXIT_USAGE
    try:
        fetched = _load(a.tickets, "tickets")
        grades = index_grades(_load(a.grades, "grades"))
        raw_actions = _load(a.actions, "actions") if a.actions and Path(a.actions).exists() else []
        if a.actions and not Path(a.actions).exists():
            print(f"[readiness] no actions file at {a.actions}: nothing was written", file=sys.stderr)
        cfg = load_config(a.config or None)
        tickets = fetched["tickets"]
        actions: dict[str, list[str]] = defaultdict(list)
        for item in raw_actions:
            actions[item["id"]].append(canonical_action(item))
    except (ValueError, KeyError, TypeError) as e:
        print(json.dumps({"error": str(e)}))
        return EXIT_USAGE
    extra = unknown_grade_ids(tickets, grades)
    if extra:
        print(f"[readiness] warning: grades for tickets that were not fetched are ignored: {', '.join(extra)}", file=sys.stderr)
    rows = evaluate(tickets, grades, cfg, actions)
    meta = {"tracker": fetched.get("tracker", ""), "scope": fetched.get("scope", ""), "dry_run": a.dry_run, "truncated": bool(fetched.get("truncated"))}
    summary_load = fetched.get("load")
    if capacity is not None and not summary_load:
        print("[readiness] warning: --capacity ignored: the fetch has no sprint load (only a sprint or milestone scope has one)", file=sys.stderr)
    sprint = {"summary": summary_load, "capacity": capacity, "flags": load.flags(summary_load, tickets, cfg, capacity)} if summary_load else None
    line = status_line(rows, meta["truncated"], len(sprint["flags"]) if sprint else 0)
    result = {**{k: v for k, v in line.items() if k != "summary"}, "verdict": line["summary"], "summary": summarize(rows), "tickets": rows, "meta": meta}
    if sprint:
        result["load"] = {**sprint["summary"], "capacity": capacity, "flags": sprint["flags"]}
    previews = load_previews(Path(a.out_dir), rows)
    write_bundle(Path(a.out_dir), "Ticket readiness", render_markdown(rows, meta, previews, sprint), result)
    (Path(a.out_dir) / "slack.md").write_text(redact(render_slack(rows, meta, previews, sprint)), encoding="utf-8")
    print(f"[readiness] wrote {a.out_dir}/report.md, report.html, result.json, slack.md", flush=True)
    print(json.dumps(line))
    return EXIT_BLOCKED if line["signal"] == "BLOCKED" else 0


if __name__ == "__main__":
    sys.exit(main())
