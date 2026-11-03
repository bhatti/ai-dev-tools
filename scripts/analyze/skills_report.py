# SPDX-License-Identifier: LGPL-2.1-or-later
"""Report of the skill / knowledge-base files a PR audit created or updated.

Written to ``reports/skill_updates_report.{md,html}`` in every mode, so the content is reviewable
from the job artifacts whether or not a PR was opened (dry run: no PR; normal run: PR link included).
"""

from __future__ import annotations

from scripts.analyze.kb_writer import KB_ROOT

_FENCE = "~~~"


def classify_path(path: str) -> str:
    if path.startswith(f"{KB_ROOT}/"):
        return "review-knowledge"
    if path.startswith(".claude/skills/"):
        return "skill"
    return "doc"


def _fenced(content: str) -> list[str]:
    # The HTML renderer toggles on any fence line, so a fence inside the content is indented to stay content.
    body = [(" " + ln) if ln.startswith(("```", "~~~")) else ln for ln in content.rstrip("\n").splitlines()]
    return [_FENCE, *body, _FENCE]


def render_skills_report_md(files: list[dict], dry_run: bool, pr_url: str, repo: str) -> str:
    """``files``: dicts with ``path``, ``action`` (create|update), ``description``, ``content``."""
    out = [f"# Skill & knowledge updates — {repo}", ""]
    if dry_run:
        out.append(":test_tube: Dry run: these files would be committed; no PR was created.")
    elif pr_url:
        out.append(f"Pull request: {pr_url}")
    else:
        out.append("No pull request was created (see the job log).")
    if not files:
        return "\n".join(out + ["", "No files were created or updated.", ""])
    out += ["", f"{len(files)} files", "", "| File | Kind | Action | Lines | Description |", "|---|---|---|---|---|"]
    for f in files:
        desc = " ".join(str(f.get("description", "")).split()).replace("|", "/")
        out.append(f"| `{f['path']}` | {classify_path(f['path'])} | {f.get('action', 'create')} | {len(f['content'].splitlines())} | {desc} |")
    for f in files:
        out += ["", f"## {f['path']}", "", *_fenced(f["content"])]
    return "\n".join(out) + "\n"
