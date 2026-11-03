# SPDX-License-Identifier: LGPL-2.1-or-later
"""Load and render ``review_learnings.json`` (what reviewers look for, by category).

The model writes ``reports/review_learnings.json`` (schema in ``ygs-pr-audit/specialists/review-learnings.md``);
``plan_skill_updates`` may write a deduplicated ``review_learnings_refined.json``. Rendering the markdown
here (not in the model) keeps the report format identical across runs.
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.analyze.kb_writer import CATEGORIES, normalize_category

_EMPTY = {"entries": [], "reviewer_profiles": []}


def load_learnings(reports_dir: Path) -> dict:
    """Refined file if valid, else the raw one, else an empty structure."""
    for name in ("review_learnings_refined.json", "review_learnings.json"):
        try:
            data = json.loads((reports_dir / name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return {"entries": data.get("entries") or [], "reviewer_profiles": data.get("reviewer_profiles") or []}
    return dict(_EMPTY, entries=[], reviewer_profiles=[])


def _line(text: object) -> str:
    return " ".join(str(text or "").split())


def render_learnings_md(data: dict, requested: tuple[str, ...], unmatched: list[str]) -> str:
    entries = data.get("entries") or []
    out = ["# Review learnings — what reviewers look for", ""]
    if requested:
        out.append(f"Reviewers analyzed: {', '.join(requested)}")
    if unmatched:
        out.append(f":warning: No PR activity found for: {', '.join(unmatched)} (check spelling; login, nickname or display name)")
    if not entries:
        return "\n".join(out + ["", "No learnings extracted from this PR set.", ""])

    by_cat: dict[str, list[dict]] = {}
    for e in entries:
        by_cat.setdefault(normalize_category(e.get("category", "")), []).append(e)
    for cat in CATEGORIES:
        items = sorted(by_cat.get(cat, []), key=lambda e: -int(e.get("pr_count") or 0))
        if not items:
            continue
        out += ["", f"## {cat.replace('-', ' ').title()}", ""]
        for e in items:
            who = f" — {', '.join(e['reviewers'])}" if e.get("reviewers") else ""
            prs = f" (PRs {', '.join(e['prs'])})" if e.get("prs") else ""
            out.append(f"- **{_line(e.get('rule'))}**{prs}{who}")
            out.append(f"  - Why: {_line(e.get('why'))}")
            if _line(e.get("look_for")):
                out.append(f"  - Look for: {_line(e['look_for'])}")

    tribal = [e for e in entries if e.get("tribal")]
    if tribal:
        out += ["", "## Tribal knowledge", "", "Rules that rest on history or system knowledge not written down elsewhere.", ""]
        out += [f"- {_line(e.get('rule'))} — {_line(e.get('why'))}" for e in tribal]

    for p in data.get("reviewer_profiles") or []:
        out += ["", f"## What {_line(p.get('reviewer'))} looks for", ""]
        areas = sorted(p.get("focus_areas") or [], key=lambda f: -int(f.get("count") or 0))
        if areas:
            out.append("Focus: " + ", ".join(f"{normalize_category(a.get('category', ''))} ({int(a.get('count') or 0)})" for a in areas))
        out += [f"- {_line(c)}" for c in p.get("signature_concerns") or []]
        for q in (p.get("quotes") or [])[:3]:
            out.append(f"- > {_line(q.get('text'))[:300]} (PR {q.get('pr', '?')})")
    return "\n".join(out) + "\n"
