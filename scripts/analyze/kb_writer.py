# SPDX-License-Identifier: LGPL-2.1-or-later
"""Build the ``review-knowledge`` skill (checklists + reviewer profiles) from audit learnings.

Pure functions: given learning entries and the files already in the audited repo, return the
files to write. Merge-only and idempotent: existing items are never rewritten or removed, a
rule already present (normalized text) is skipped, and files that would not change are
omitted. ``create_skill_pr`` writes the result into the PR (or into ``reports/dry_run/``).

Layout (one short file per category, so each stays reviewable and DRY)::

    .claude/skills/review-knowledge/SKILL.md            index, links only
    .claude/skills/review-knowledge/checklists/<category>.md
    .claude/skills/review-knowledge/reviewers/<handle>.md   (``--reviewers`` runs)

Item format and categories: ``you-got-skills/skills/shared/review-knowledge-format.md``.
"""

from __future__ import annotations

import re

KB_ROOT = ".claude/skills/review-knowledge"

CATEGORIES = ("architecture", "security", "sre", "testing", "api-compat", "correctness",
              "performance", "style", "spec", "process")

_ALIASES = {
    "arch": "architecture", "design": "architecture", "arch/design": "architecture", "architecture/design": "architecture",
    "ops": "sre", "sre/ops": "sre", "reliability": "sre", "observability": "sre",
    "test": "testing", "tests": "testing",
    "api": "api-compat", "compat": "api-compat", "api/compat": "api-compat", "api-compatibility": "api-compat",
    "bug": "correctness", "logic": "correctness",
    "perf": "performance", "perf/scale": "performance", "scale": "performance",
    "code-style": "style", "code-style/idiom": "style", "idiom": "style", "readability": "style",
    "requirements": "spec", "spec/requirements": "spec",
}

_ITEM = "- [ ] "
_SEP = " — "


def normalize_category(raw: str) -> str:
    key = (raw or "").strip().lower().replace(" ", "-")
    if key in CATEGORIES:
        return key
    return _ALIASES.get(key, "process")


def _one_line(text: object, limit: int = 400) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _key(rule: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (rule or "").lower()).strip()


def select_entries(entries: list[dict], priority_reviewers: tuple[str, ...] = (), min_prs: int = 2) -> list[dict]:
    """Keep entries with a rule and a why that are backed by ``min_prs`` PRs, or come from a requested
    senior reviewer, or are flagged tribal (history that no test or doc captures)."""
    prio = {p.lower() for p in priority_reviewers}
    kept = []
    for e in entries:
        if not _one_line(e.get("rule")) or not _one_line(e.get("why")):
            continue
        by_prio = bool(prio & {str(r).lower() for r in e.get("reviewers", [])})
        if int(e.get("pr_count") or 0) >= min_prs or by_prio or e.get("tribal"):
            kept.append(e)
    return kept


def _render_item(e: dict) -> str:
    parts = [_one_line(e["rule"]), f"Why: {_one_line(e.get('why'))}"]
    if _one_line(e.get("look_for")):
        parts.append(f"Look for: {_one_line(e['look_for'])}")
    return _ITEM + _SEP.join(parts) + (" (tribal)" if e.get("tribal") else "")


def _existing_keys(text: str) -> set[str]:
    keys = set()
    for line in text.splitlines():
        if line.startswith(_ITEM):
            keys.add(_key(line[len(_ITEM):].split(_SEP + "Why:")[0]))
    return keys


def _count_items(text: str) -> int:
    return sum(1 for ln in text.splitlines() if ln.startswith(_ITEM))


def merge_checklist(existing_text: str, category: str, entries: list[dict]) -> tuple[str, int]:
    """Append new items to a category checklist; returns ``(text, added)`` (text unchanged when added == 0)."""
    seen = _existing_keys(existing_text)
    new_lines = []
    for e in entries:
        k = _key(e.get("rule", ""))
        if k and k not in seen:
            seen.add(k)
            new_lines.append(_render_item(e))
    if not new_lines:
        return existing_text, 0
    base = existing_text.rstrip("\n") if existing_text else (
        f"# {category.replace('-', ' ').title()} review checklist\n\n"
        "Rules learned from senior reviewers' PR feedback. Each item says why it matters and what to look for.\n"
    )
    return base + "\n" + "\n".join(new_lines) + "\n", len(new_lines)


def _handle(name: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-") or "reviewer"


def _profile_concerns(existing: str) -> list[str]:
    out, active = [], False
    for line in existing.splitlines():
        if line.startswith("## "):
            active = line.strip() == "## Signature concerns"
        elif active and line.startswith("- "):
            out.append(line[2:].strip())
    return out


def _render_profile(p: dict, existing: str) -> str:
    name = _one_line(p.get("reviewer"), 80)
    concerns, seen = [], set()
    for c in _profile_concerns(existing) + [_one_line(c) for c in p.get("signature_concerns", [])]:
        if c and _key(c) not in seen:
            seen.add(_key(c))
            concerns.append(c)
    lines = [f"# What {name} looks for", "",
             "Derived from this reviewer's PR comments; the rules live in the checklists, this file only points to them.", "",
             "## Focus areas", ""]
    for fa in sorted(p.get("focus_areas", []), key=lambda f: -int(f.get("count") or 0)):
        cat = normalize_category(fa.get("category", ""))
        lines.append(f"- [{cat}](../checklists/{cat}.md) — {int(fa.get('count') or 0)} comments")
    lines += ["", "## Signature concerns", ""] + [f"- {c}" for c in concerns]
    quotes = [q for q in p.get("quotes", []) if _one_line(q.get("text"))][:3]
    if quotes:
        lines += ["", "## Example feedback", ""] + [f"- \"{_one_line(q['text'], 300)}\"" for q in quotes]
    return "\n".join(lines) + "\n"


def _render_index(counts: dict[str, int], reviewers: list[str]) -> str:
    lines = ["---", "name: review-knowledge",
             "description: Review checklists distilled from senior engineers' PR feedback (architecture, security, SRE, testing, API, correctness, performance, style). Apply when reviewing or writing code.",
             "---", "", "# Review knowledge", "",
             "Walk the checklists that match the change; each item states the rule, why it matters and what to look for.",
             "Generated by pr-audit from reviewers' PR comments and added through a reviewed PR; edit freely, items are never rewritten by later runs.", "",
             "| Checklist | Items |", "|---|---|"]
    lines += [f"| [{c}](checklists/{c}.md) | {counts[c]} |" for c in CATEGORIES if counts.get(c)]
    if reviewers:
        lines += ["", "## Reviewer profiles", ""] + [f"- [{r}](reviewers/{r}.md)" for r in sorted(reviewers)]
    return "\n".join(lines) + "\n"


def build_kb_files(entries: list[dict], profiles: list[dict], existing: dict[str, str]) -> dict[str, str]:
    """Return ``{repo-relative path: content}`` for files that are new or would change."""
    out: dict[str, str] = {}
    by_cat: dict[str, list[dict]] = {}
    for e in entries:
        by_cat.setdefault(normalize_category(e.get("category", "")), []).append(e)

    final_checklists = {p: t for p, t in existing.items() if p.startswith(f"{KB_ROOT}/checklists/")}
    for cat, items in by_cat.items():
        path = f"{KB_ROOT}/checklists/{cat}.md"
        text, added = merge_checklist(existing.get(path, ""), cat, items)
        if added:
            out[path] = text
            final_checklists[path] = text

    final_reviewers = {p for p in existing if p.startswith(f"{KB_ROOT}/reviewers/")}
    for prof in profiles:
        if not _one_line(prof.get("reviewer")):
            continue
        path = f"{KB_ROOT}/reviewers/{_handle(prof['reviewer'])}.md"
        text = _render_profile(prof, existing.get(path, ""))
        if text != existing.get(path):
            out[path] = text
        final_reviewers.add(path)

    if out:
        counts = {c: _count_items(final_checklists.get(f"{KB_ROOT}/checklists/{c}.md", "")) for c in CATEGORIES}
        handles = [p.rsplit("/", 1)[1][:-3] for p in final_reviewers]
        index_path = f"{KB_ROOT}/SKILL.md"
        index = _render_index(counts, handles)
        if index != existing.get(index_path):
            out[index_path] = index
    return out
