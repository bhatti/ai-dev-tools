"""Skill discovery — find SKILL.md files across all configured skill locations.

Search order (first match wins for find_skill; all dirs scored for find_skill_for_query):
  1. $CODEBASE_DIR/.claude/skills/<name>/SKILL.md
  2. For each dir in EXTRA_SKILLS_REPOS (colon-sep, semicolon-sep, comma-sep, or JSON list):
     <dir>/skills/<name>/SKILL.md  and  <dir>/<name>/SKILL.md
  3. ~/.claude/skills/<name>/SKILL.md
  4. ~/.claude/skills/you-got-skills/skills/<name>/SKILL.md

Config keys are checked in both CamelCase (ExtraSkillsRepos) and UPPER_SNAKE (EXTRA_SKILLS_REPOS)
so that Formicary org-config settings and environment variables both work without special handling.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def _get_extra_skill_repos(config: dict | None) -> list[str]:
    """Return list of extra skill repo root paths from config.

    Accepts both EXTRA_SKILLS_REPOS (env/UPPER_SNAKE) and ExtraSkillsRepos (org-config CamelCase).
    Supports colon-, semicolon-, comma-separated paths and JSON arrays.
    """
    raw = ""
    if config:
        raw = config.get("EXTRA_SKILLS_REPOS") or config.get("ExtraSkillsRepos") or ""
    if not raw:
        raw = os.environ.get("EXTRA_SKILLS_REPOS", "")
    if not raw:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        import json
        try:
            return [p.strip() for p in json.loads(raw) if isinstance(p, str) and p.strip()]
        except Exception:
            pass
    return [p.strip() for p in re.split(r"[;:,]", raw) if p.strip()]


def _skill_search_dirs(config: dict | None = None) -> list[Path]:
    """Return ordered list of skill base directories to search."""
    dirs: list[Path] = []

    # 1. Codebase-local skills (highest priority)
    codebase = os.environ.get("CODEBASE_DIR", "").strip()
    if codebase:
        dirs.append(Path(codebase) / ".claude" / "skills")

    # 2. Extra skill repos
    for repo_root in _get_extra_skill_repos(config):
        p = Path(repo_root)
        dirs.append(p / "skills")  # <repo>/skills/<name>/SKILL.md
        dirs.append(p)             # <repo>/<name>/SKILL.md (if already a skills/ dir)

    # 3. User home dirs
    home = Path.home()
    dirs.append(home / ".claude" / "skills")
    dirs.append(home / ".claude" / "skills" / "you-got-skills" / "skills")

    return dirs


def find_skill(name: str, config: dict | None = None) -> Path | None:
    """Find a SKILL.md by exact skill name across all configured skill locations.

    Returns the Path to the first matching SKILL.md, or None if not found.
    """
    for base in _skill_search_dirs(config):
        candidate = base / name / "SKILL.md"
        if candidate.exists():
            print(f"[skill_resolver] found '{name}' at {candidate}", flush=True)
            return candidate
    print(f"[skill_resolver] skill '{name}' not found in any configured location", flush=True)
    return None


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract simple key: value pairs from YAML frontmatter (string fields only)."""
    result: dict[str, str] = {}
    if not text.startswith("---"):
        return result
    end = text.find("---", 3)
    if end == -1:
        return result
    for line in text[3:end].splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            result[key.strip()] = val.strip().strip('"').strip("'")
    return result


def _score_skill(skill_text: str, query_tokens: set[str]) -> int:
    """Score a skill against query tokens using keyword overlap."""
    fm = _parse_frontmatter(skill_text)
    haystack = " ".join([
        fm.get("name", ""),
        fm.get("description", ""),
        fm.get("keywords", ""),
    ]).lower()
    # Also scan the "When to use" section in the skill body (first 600 chars)
    haystack += " " + skill_text[:600].lower()
    return sum(1 for tok in query_tokens if tok in haystack)


def find_skill_for_query(query: str, config: dict | None = None) -> tuple[str, Path] | None:
    """Find the most relevant skill for a free-text query.

    Scans all skill directories, scores each SKILL.md against the query tokens,
    and returns (skill_name, skill_path) for the best match above a relevance
    threshold, or None if no skill is relevant enough.

    The matching is fully keyword-driven — no analysis type is hardcoded.
    """
    # Tokenize: lowercase words 3+ chars, minus common stop words
    stop = {
        "the", "and", "for", "this", "that", "with", "are", "has", "its", "not",
        "but", "can", "all", "any", "how", "what", "why", "from", "have", "been",
        "was", "were", "will", "they", "their", "about", "into", "also",
    }
    tokens = {w for w in re.findall(r"[a-z]{3,}", query.lower()) if w not in stop}
    if not tokens:
        return None

    best_score = 0
    best_name: str | None = None
    best_path: Path | None = None

    seen: set[Path] = set()
    for base in _skill_search_dirs(config):
        if not base.exists():
            continue
        for skill_dir in sorted(base.iterdir()):  # sorted for determinism
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists() or skill_md.resolve() in seen:
                continue
            seen.add(skill_md.resolve())
            try:
                text = skill_md.read_text(encoding="utf-8")
            except Exception:
                continue
            score = _score_skill(text, tokens)
            if score > best_score:
                best_score = score
                best_name = skill_dir.name
                best_path = skill_md

    threshold = 2
    if best_score >= threshold and best_name and best_path:
        print(
            f"[skill_resolver] matched '{best_name}' (score={best_score}) "
            f"for query: {query[:80]}",
            flush=True,
        )
        return (best_name, best_path)

    print(
        f"[skill_resolver] no relevant skill found (best_score={best_score}) "
        "— falling back to git archaeology",
        flush=True,
    )
    return None
