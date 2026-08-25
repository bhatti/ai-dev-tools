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
from pathlib import Path


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
