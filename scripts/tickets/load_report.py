"""The sprint load block of the readiness report: Markdown / HTML for people, mrkdwn for Slack, action items for owners.

Everything shown comes from the totals computed by ``scripts.tickets.load``; nothing is added up here.
"""

from __future__ import annotations

from scripts.common.report_bundle import md_cell
from scripts.common.sprint_load import LANE_LABELS, LANES, format_line
from scripts.tickets import slack_text

_LANE_TITLES = {"pending": "Pending", "in_progress": "In progress", "review": "Review", "done": "Done"}
_CARRY_TITLES = {"pending": "Pending (never started)", "in_progress": "In progress", "review": "Review", "done": "Done"}


def _pts(value: float) -> str:
    return f"{value:g}"


def _flag(flags: list[dict], name: str) -> dict | None:
    return next((f for f in flags if f["flag"] == name), None)


def _capacity_line(summary: dict, flags: list[dict], capacity: float | None) -> str:
    if capacity is None:
        return ""
    over = _flag(flags, "over_capacity")
    base = f"Open load (pending + in progress + review): {_pts(summary['open']['points'])} pts vs capacity {_pts(capacity)}"
    return f"{base}: **over by {_pts(over['over_by'])}**" if over else f"{base}: within capacity"


def _cap(values: list[str], shown: int = 30) -> str:
    """Ids joined, at most ``shown`` of them and a count of the rest."""
    return ", ".join(values[:shown]) + (f" (+{len(values) - shown} more)" if len(values) > shown else "")


def markdown(summary: dict, flags: list[dict], capacity: float | None) -> list[str]:
    total = summary["total"]
    lines = ["## Sprint load", "", "| Lane | Tickets | Points | % of points | Without points |", "| --- | ---: | ---: | ---: | --- |"]
    for lane in LANES:
        b = summary["lanes"][lane]
        lines.append(f"| {_LANE_TITLES[lane]} | {b['count']} | {_pts(b['points'])} | {summary['share'][lane]:.0%} | {md_cell(', '.join(b['unpointed']) or '—')} |")
    lines.append(f"| **Total** | {total['count']} | {_pts(total['points'])} |  | {total['unpointed']} ticket(s) |")
    capacity_line = _capacity_line(summary, flags, capacity)
    if capacity_line:
        lines += ["", capacity_line]
    carry = summary["carry_over"]
    if carry:
        note = " (above the carry-over heuristic)" if _flag(flags, "carry_share") else ""
        lines += ["", "### Carry-over", "", f"{carry['count']} ticket(s) · {_pts(carry['points'])} pts · {carry['share_of_sprint']:.0%} of the sprint{note}", ""]
        if carry["count"]:
            lines += ["| Lane | Tickets |", "| --- | --- |"]
            for lane in LANES:
                ids = [f"{i} ({carry['times'][i]}×)" for i in carry["by_lane"][lane]]
                if ids:
                    lines.append(f"| {_CARRY_TITLES[lane]} | {md_cell(_cap(ids))} |")
            if carry["chronic"]:
                lines += ["", f"**Chronic** (not done after two or more earlier sprints): {md_cell(_cap(carry['chronic']))}"]
    roll = summary["pending_rollover"]
    if roll:
        hidden = [t for t in roll["tickets"] if not t["already_in_target"]]
        lines += ["", "### Hidden rollover", "", f"Unfinished in {md_cell(roll['from_sprint'])} and what will roll over if nobody acts: {len(hidden)} ticket(s) missing from this sprint."]
        if hidden:
            lines += ["", "| Ticket | Lane | Points |", "| --- | --- | ---: |"]
            lines += [f"| {md_cell(t['id'])} | {LANE_LABELS[t['lane']]} | {_pts(t['story_points']) if t['story_points'] else '—'} |" for t in hidden]
    if summary["notes"]:
        lines += [""] + [f"Note: {md_cell(n)}" for n in summary["notes"]]
    if flags:
        lines += ["", "### To decide", ""]
        for f in flags:
            who = f" ({md_cell(_cap(f['ids']))})" if f["ids"] else ""
            lines.append(f"- **{f['ask']}**{who}")
    return lines


def slack(summary: dict, flags: list[dict], capacity: float | None) -> list[str]:
    """Short mrkdwn lines: the lane line, the capacity verdict, carry-over, and one line per flag."""
    lines = [f"*Sprint load* — {format_line(summary)}"]
    over = _flag(flags, "over_capacity")
    if capacity is not None:
        lines.append(f"Open {_pts(summary['open']['points'])} pts vs capacity {_pts(capacity)}: " + (f"over by {_pts(over['over_by'])}" if over else "within capacity"))
    carry = summary["carry_over"]
    if carry:
        lines.append(f"Carry-over: {carry['count']} ticket(s) · {_pts(carry['points'])} pts · {carry['share_of_sprint']:.0%} of the sprint")
    names = {"unpointed": "Without points", "off_scale": "Off-scale or too big", "chronic_carry_over": "Not done after 2+ sprints", "carried_not_started": "Carried over, never started",
             "carried_in_flight": "Carried over, in flight (re-point?)", "hidden_rollover": f"Unfinished in {slack_text.escape((summary['pending_rollover'] or {}).get('from_sprint', 'the active sprint'))} and missing here"}
    for f in flags:
        if f["flag"] in names and f["ids"]:
            lines.append(f"• {names[f['flag']]}: {slack_text.ids(f['ids'])}")
    return lines


def action_items(summary: dict, flags: list[dict]) -> list[tuple[str, list[str]]]:
    """(who, ids) for the decisions the load raises, in the same shape as the per-ticket action items."""
    out = []
    team = sorted({i for f in flags if f["flag"] in ("chronic_carry_over", "carried_not_started") for i in f["ids"]})
    if team:
        out.append(("Team (carry-over decisions: split, re-scope or drop)", team))
    hidden = _flag(flags, "hidden_rollover")
    if hidden:
        out.append(("Tech lead (unfinished work missing from this sprint)", hidden["ids"]))
    return out
