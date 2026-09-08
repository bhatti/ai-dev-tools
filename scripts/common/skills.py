# SPDX-License-Identifier: LGPL-2.1-or-later
"""Project-level skill overlay.

After cloning a project repo, call apply_project_skills(repo_dir) to symlink
any skills found in <repo>/.claude/skills/ into ~/.claude/skills/, overriding
the ygs base skills of the same name.  This mirrors the /etc/init.d pattern:
entrypoint.sh installs the base skills on container start; this function applies
project-specific overrides once the repo is available on disk.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def inline_shared_refs(content: str) -> str:
    """Resolve 'read `~/.claude/skills/.../shared/<name>.md`' references and inline them.

    Skill files may reference shared protocols (review-scaffold.md, dep-audit.md, etc.)
    via backtick-quoted paths. Claude may skip reading those files due to token-efficiency
    rules. Inlining ensures the full protocol is always present in the prompt.

    Two-pass resolution handles transitive refs (e.g. scaffold → ownership-principles).
    """
    shared_dirs = [
        Path.home() / ".claude" / "skills" / "you-got-skills" / "skills" / "shared",
        Path.home() / ".claude" / "skills" / "shared",
    ]
    pattern = re.compile(
        r'[Rr]ead\s+`[^`]*?/?shared/([a-z0-9_-]+\.md)`[^.\n]*\.?'
    )
    inlined: set[str] = set()

    def _replace(match: re.Match) -> str:
        filename = match.group(1)
        if filename in inlined:
            return f"(See inlined {filename} above.)"
        for d in shared_dirs:
            path = d / filename
            if path.exists():
                text = path.read_text(encoding="utf-8").strip()
                inlined.add(filename)
                print(f"[skills] inlined shared/{filename} ({len(text)} chars)", flush=True)
                return f"\n\n<!-- inlined from shared/{filename} -->\n{text}\n"
        return match.group(0)

    resolved = pattern.sub(_replace, content)
    # Second pass: inlined files may reference other shared files transitively
    if inlined:
        resolved = pattern.sub(_replace, resolved)
    if inlined:
        print(f"[skills] inlined {len(inlined)} shared file(s): {sorted(inlined)}", flush=True)
    return resolved


def apply_project_skills(repo_dir: Path) -> int:
    """Symlink project skills from <repo_dir>/.claude/skills/ into ~/.claude/skills/.

    Project skills override base ygs skills of the same name (ln -snf).
    Skills present only in the base set are untouched.

    Returns the number of skills applied.
    """
    proj_skills = repo_dir / ".claude" / "skills"
    if not proj_skills.is_dir():
        return 0

    skills_base = Path.home() / ".claude" / "skills"
    skills_base.mkdir(parents=True, exist_ok=True)

    count = 0
    for skill_dir in sorted(proj_skills.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_name = skill_dir.name
        target = skills_base / skill_name
        # Resolve to absolute path so the symlink stays valid regardless of cwd
        target.unlink(missing_ok=True)
        target.symlink_to(skill_dir.resolve())
        print(f"[skills] project override: {skill_name} → {skill_dir.resolve()}", flush=True)
        count += 1

    if count:
        print(f"[skills] {count} project skill(s) applied from {proj_skills}", flush=True)
    return count
