"""Collapse repeated QA evidence so a report states each problem once, with a count.

Display-only: nothing here changes case outcomes, counts or the verdict. The same root cause (one broken endpoint, one
console error on a shared page) otherwise shows up once per case in the table, the headline and the delta.

  * ``dedup_failures``: failing / blocked / inconclusive cases whose messages normalise to the same text are one group
    (volatile tokens such as timestamps, ids, ports and durations are ignored; HTTP status codes are kept apart).
  * ``dedup_exchanges``: identical requests with an identical response prefix inside one case (a polling loop, a retry)
    are shown once with a count. The same exchange in different cases is kept per case: an expected 401 is legitimately
    repeated by several scenarios and each one needs its own evidence.
"""

from __future__ import annotations

from scripts.common.text_utils import normalize_log_line

_BODY_KEY_CHARS = 240


def clip(text: str, limit: int = 1000) -> str:
    """Cut ``text`` to ``limit`` characters and say how much was left out."""
    text = text or ""
    return text if len(text) <= limit else f"{text[:limit]}... ({len(text) - limit} more characters)"


def dedup_failures(outcomes: dict[str, dict]) -> list[dict]:
    """Groups of 2+ cases that failed the same way: ``{signature, outcome, count, cases, sample}``, largest first."""
    groups: dict[tuple[str, str], dict] = {}
    for cid, o in outcomes.items():
        if o["outcome"] == "pass":
            continue
        for message in dict.fromkeys(o.get("messages") or []):
            sig = normalize_log_line(message)
            g = groups.setdefault((o["outcome"], sig), {"signature": sig, "outcome": o["outcome"], "cases": [], "sample": message[:240]})
            if cid not in g["cases"]:
                g["cases"].append(cid)
    found = [{**g, "count": len(g["cases"])} for g in groups.values() if len(g["cases"]) > 1]
    return sorted(found, key=lambda g: (-g["count"], g["signature"]))


def dedup_exchanges(records: list[dict]) -> list[dict]:
    """Identical non-passing exchanges inside one case collapsed: ``{case, outcome, command, observed, count}`` in first-seen order."""
    seen: dict[tuple, dict] = {}
    for r in records:
        if r["outcome"] == "pass":
            continue
        key = (r["case"], r["outcome"], r.get("command", ""), (r.get("observed") or "")[:_BODY_KEY_CHARS])
        if key in seen:
            seen[key]["count"] += 1
        else:
            seen[key] = {"case": r["case"], "outcome": r["outcome"], "command": r.get("command", ""), "observed": r.get("observed", ""), "count": 1}
    return list(seen.values())
