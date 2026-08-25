# SPDX-License-Identifier: LGPL-2.1-or-later
"""Tests for scripts.common.skills.apply_project_skills."""

from pathlib import Path

import pytest

from scripts.common.skills import apply_project_skills


def _make_skill(base: Path, name: str) -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"# {name}", encoding="utf-8")
    return skill_dir


def test_no_dot_claude_skills_dir(tmp_path):
    """apply_project_skills returns 0 when .claude/skills does not exist."""
    repo = tmp_path / "repo"
    repo.mkdir()
    assert apply_project_skills(repo) == 0


def test_empty_skills_dir(tmp_path):
    """apply_project_skills returns 0 when .claude/skills is empty."""
    repo = tmp_path / "repo"
    (repo / ".claude" / "skills").mkdir(parents=True)
    assert apply_project_skills(repo) == 0


def test_skills_symlinked(tmp_path, monkeypatch):
    """Skills in .claude/skills are symlinked into ~/.claude/skills."""
    repo = tmp_path / "repo"
    proj_skills = repo / ".claude" / "skills"
    proj_skills.mkdir(parents=True)
    _make_skill(proj_skills, "ygs-custom")
    _make_skill(proj_skills, "my-skill")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    count = apply_project_skills(repo)

    assert count == 2
    installed = fake_home / ".claude" / "skills"
    assert (installed / "ygs-custom").is_symlink()
    assert (installed / "my-skill").is_symlink()
    assert (installed / "ygs-custom" / "SKILL.md").read_text() == "# ygs-custom"


def test_project_skill_overrides_base(tmp_path, monkeypatch):
    """Project skill wins over a pre-existing base symlink of the same name."""
    fake_home = tmp_path / "home"
    installed = fake_home / ".claude" / "skills"
    installed.mkdir(parents=True)

    # Pre-install a base ygs skill
    base_skill = tmp_path / "ygs-base" / "ygs-review-pr"
    base_skill.mkdir(parents=True)
    (base_skill / "SKILL.md").write_text("# base version", encoding="utf-8")
    (installed / "ygs-review-pr").symlink_to(base_skill)

    # Project has its own override
    repo = tmp_path / "repo"
    proj_skills = repo / ".claude" / "skills"
    proj_skills.mkdir(parents=True)
    proj_override = _make_skill(proj_skills, "ygs-review-pr")
    (proj_override / "SKILL.md").write_text("# project version", encoding="utf-8")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    count = apply_project_skills(repo)

    assert count == 1
    link = installed / "ygs-review-pr"
    assert link.is_symlink()
    assert (link / "SKILL.md").read_text() == "# project version"


def test_non_dir_entries_ignored(tmp_path, monkeypatch):
    """Files (not directories) in .claude/skills are ignored."""
    repo = tmp_path / "repo"
    proj_skills = repo / ".claude" / "skills"
    proj_skills.mkdir(parents=True)
    (proj_skills / "README.md").write_text("not a skill", encoding="utf-8")
    _make_skill(proj_skills, "ygs-real")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    count = apply_project_skills(repo)
    assert count == 1
