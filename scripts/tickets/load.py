"""Sprint load for sprint-ready: story points per lane, carry-over, hidden rollover, and the flags a planner acts on.

``sprint_summary`` runs at fetch time over every ticket in the sprint (finished ones included, they are the velocity), ``flags`` at
report time with the project's settings. Totals come from ``scripts.common.sprint_load``; nothing here asks the model to add points.
"""

from __future__ import annotations

from scripts.common import sprint_load
from scripts.tickets.config import ReadinessConfig

SPLIT_AT = 21  # points at or above this mean "too big: split it"


def _item(t: dict) -> dict:
    return {"id": t["id"], "lane": t["lane"], "story_points": t["story_points"]["value"], "is_subtask": t["is_subtask"], "assignee": t["assignee"]}


NO_HISTORY = "carry-over was not computed: the tracker gives no sprint history for this scope (or the sprint was not found)"


def sprint_summary(tickets: list[dict], target: dict | None, rollover: dict | None, no_history: str = NO_HISTORY) -> dict:
    """Totals per lane plus carry-over (tickets that were in an earlier sprint, by sprint id) for ``target``, the sprint being prepared.
    Sets ``carry_over`` on every ticket. Carry-over is None, with a note, when there is no target sprint to compare against."""
    summary = sprint_load.summarize(_item(t) for t in tickets)
    unknown = [t["id"] for t in tickets if not t["is_subtask"] and not t["story_points"]["known"]]
    summary["points_unknown"] = unknown
    notes: list[str] = []
    if unknown:
        notes.append(f"story points could not be read from the tracker for {len(unknown)} ticket(s); the point totals and capacity check are not reliable")
    carry = None
    if target:
        carried = []
        for t in tickets:
            prior = sorted((s for s in t["sprints"] if s["id"] < target["id"]), key=lambda s: s["id"])
            t["carry_over"] = {"times": len(prior), "from_sprints": [s["name"] for s in prior]}
            if prior and not t["is_subtask"]:
                carried.append(t)
        points = sum(t["story_points"]["value"] or 0 for t in carried)
        carry = {
            "count": len(carried), "points": points, "unpointed": [t["id"] for t in carried if not t["story_points"]["value"]],
            "share_of_sprint": points / summary["total"]["points"] if summary["total"]["points"] else 0.0,
            "by_lane": {lane: [t["id"] for t in carried if t["lane"] == lane] for lane in sprint_load.LANES},
            "chronic": [t["id"] for t in carried if t["carry_over"]["times"] >= 2 and t["lane"] != "done"],
            "times": {t["id"]: t["carry_over"]["times"] for t in carried},
        }
    else:
        notes.append(no_history)
    return {**summary, "target_sprint": target, "carry_over": carry, "pending_rollover": rollover, "notes": notes}


def flags(summary: dict, tickets: list[dict], cfg: ReadinessConfig, capacity: float | None) -> list[dict]:
    """What a planner must decide, each with the question to ask. Empty for a clean sprint."""
    out: list[dict] = []
    real = [t for t in tickets if not t["is_subtask"]]
    unknown = set(summary.get("points_unknown", []))
    unpointed = [i for lane in sprint_load.LANES for i in summary["lanes"][lane]["unpointed"] if i not in unknown]
    if unknown:
        out.append({"flag": "points_unknown", "ids": sorted(unknown), "ask": "Story points could not be read (field list or project field unavailable): fix access and re-run before trusting totals or capacity"})
    if unpointed:
        out.append({"flag": "unpointed", "ids": unpointed, "ask": "Add points; for finished tickets add them now so the sprint's velocity is true"})
    off = [t["id"] for t in real if t["lane"] != "done" and t["story_points"]["value"] and (t["story_points"]["value"] >= SPLIT_AT or t["story_points"]["value"] not in cfg.points_scale)]
    if off:
        out.append({"flag": "off_scale", "ids": off, "ask": "Re-point on the scale, or split anything of 21 points or more"})
    open_points = summary["open"]["points"]
    if capacity is not None and open_points > capacity and not unknown:
        out.append({"flag": "over_capacity", "ids": [], "open_points": open_points, "capacity": capacity, "over_by": open_points - capacity,
                    "ask": "Which tickets leave the sprint? (open work = pending + in progress + review)"})
    carry = summary["carry_over"]
    if carry:
        if carry["share_of_sprint"] > cfg.carry_over_share:
            out.append({"flag": "carry_share", "ids": [], "share": round(carry["share_of_sprint"], 4), "threshold": cfg.carry_over_share,
                        "ask": f"Carried-over points are above {cfg.carry_over_share:.0%} of the sprint (a heuristic): is it really planned, or mostly last sprint's leftovers?"})
        if carry["chronic"]:
            out.append({"flag": "chronic_carry_over", "ids": carry["chronic"], "ask": "Not done after two or more earlier sprints: split, re-scope or drop, and name what blocked it each time"})
        if carry["by_lane"]["pending"]:
            out.append({"flag": "carried_not_started", "ids": carry["by_lane"]["pending"], "ask": "Why did it not start: blocked, unclear spec, or deprioritized?"})
        in_flight = carry["by_lane"]["in_progress"] + carry["by_lane"]["review"]
        if in_flight:
            out.append({"flag": "carried_in_flight", "ids": in_flight, "ask": "Its points are the full estimate: re-point to the remaining work so the load is not overstated?"})
    roll = summary["pending_rollover"]
    hidden = [t for t in (roll or {}).get("tickets", []) if not t["already_in_target"]]
    if hidden:
        out.append({"flag": "hidden_rollover", "ids": [t["id"] for t in hidden], "points": sum(t["story_points"] or 0 for t in hidden), "from_sprint": roll["from_sprint"],
                    "ask": "Unfinished in the active sprint and absent from this one: pull in, drop, or hand off"})
    return out
