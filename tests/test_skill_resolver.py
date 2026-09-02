"""Tests for scripts/common/skill_resolver.py"""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts.common.skill_resolver import (
    _get_extra_skill_repos,
    find_skill,
    find_skill_for_query,
)


def _make_skill(base: Path, name: str, description: str = "") -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f'---\nname: {name}\ndescription: "{description}"\n---\n# {name}\n',
        encoding="utf-8",
    )
    return skill_md


def test_find_skill_in_home_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEBASE_DIR", "")
    monkeypatch.setenv("EXTRA_SKILLS_REPOS", "")
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    _make_skill(skills_dir, "ygs-analyze", "flaky test detection")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "home", lambda: tmp_path)
        result = find_skill("ygs-analyze")
    assert result is not None
    assert result.name == "SKILL.md"


def test_find_skill_extra_repos(tmp_path, monkeypatch):
    extra_skills = tmp_path / "extra" / "skills"
    extra_skills.mkdir(parents=True)
    expected = _make_skill(extra_skills, "ygs-myskill", "custom skill")
    monkeypatch.setenv("EXTRA_SKILLS_REPOS", str(tmp_path / "extra"))
    monkeypatch.setenv("CODEBASE_DIR", "")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "home", lambda: tmp_path / "fakehome")
        result = find_skill("ygs-myskill")
    assert result == expected


def test_extra_skills_repos_camelcase():
    config = {"ExtraSkillsRepos": "/opt/skills1:/opt/skills2"}
    paths = _get_extra_skill_repos(config)
    assert "/opt/skills1" in paths
    assert "/opt/skills2" in paths


def test_extra_skills_repos_upper_snake():
    config = {"EXTRA_SKILLS_REPOS": "/opt/a,/opt/b"}
    paths = _get_extra_skill_repos(config)
    assert "/opt/a" in paths
    assert "/opt/b" in paths


def test_extra_skills_repos_json_list():
    config = {"EXTRA_SKILLS_REPOS": '["/opt/x", "/opt/y"]'}
    paths = _get_extra_skill_repos(config)
    assert "/opt/x" in paths
    assert "/opt/y" in paths


def test_extra_skills_repos_camelcase_takes_precedence_over_env(monkeypatch):
    monkeypatch.setenv("EXTRA_SKILLS_REPOS", "/env/path")
    config = {"ExtraSkillsRepos": "/config/path1:/config/path2"}
    # config takes precedence over env
    paths = _get_extra_skill_repos(config)
    assert "/config/path1" in paths


def test_find_skill_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEBASE_DIR", "")
    monkeypatch.setenv("EXTRA_SKILLS_REPOS", "")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "home", lambda: tmp_path)
        result = find_skill("nonexistent-skill-xyz")
    assert result is None


def test_find_skill_for_query_matches_by_description(tmp_path, monkeypatch):
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    _make_skill(skills_dir, "ygs-analyze", "Deep code analysis flaky test detection performance profiling")
    _make_skill(skills_dir, "ygs-risk-scan", "Security vulnerability CVE risk scanning")
    monkeypatch.setenv("CODEBASE_DIR", "")
    monkeypatch.setenv("EXTRA_SKILLS_REPOS", "")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "home", lambda: tmp_path)
        result = find_skill_for_query("flaky test detection intermittent failures")
    assert result is not None
    assert result[0] == "ygs-analyze"


def test_find_skill_for_query_security_matches_risk_scan(tmp_path, monkeypatch):
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    _make_skill(skills_dir, "ygs-analyze", "code analysis performance flaky tests")
    _make_skill(skills_dir, "ygs-risk-scan", "security vulnerability CVE risk scanning audit")
    monkeypatch.setenv("CODEBASE_DIR", "")
    monkeypatch.setenv("EXTRA_SKILLS_REPOS", "")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "home", lambda: tmp_path)
        result = find_skill_for_query("security vulnerability CVE audit dependencies")
    assert result is not None
    assert result[0] == "ygs-risk-scan"


def test_find_skill_for_query_returns_none_below_threshold(tmp_path, monkeypatch):
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    _make_skill(skills_dir, "ygs-analyze", "code analysis")
    monkeypatch.setenv("CODEBASE_DIR", "")
    monkeypatch.setenv("EXTRA_SKILLS_REPOS", "")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "home", lambda: tmp_path)
        result = find_skill_for_query("zyx qrw mnop totally unrelated query terms")
    assert result is None


def test_find_skill_for_query_returns_none_on_empty_query(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEBASE_DIR", "")
    monkeypatch.setenv("EXTRA_SKILLS_REPOS", "")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "home", lambda: tmp_path)
        result = find_skill_for_query("")
    assert result is None


def test_find_skill_deduplicates_across_search_dirs(tmp_path, monkeypatch):
    """Same physical SKILL.md reachable via two paths should only be scored once."""
    skills_dir = tmp_path / ".claude" / "skills"
    skills_dir.mkdir(parents=True)
    _make_skill(skills_dir, "ygs-analyze", "flaky test detection performance analysis")
    # Point EXTRA_SKILLS_REPOS at the same .claude dir — creates a duplicate search path
    monkeypatch.setenv("EXTRA_SKILLS_REPOS", str(tmp_path / ".claude"))
    monkeypatch.setenv("CODEBASE_DIR", "")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "home", lambda: tmp_path)
        result = find_skill_for_query("flaky test performance analysis profiling")
    assert result is not None  # should still find it, not crash
